"""The host <-> KWin link (ARCHITECTURE §4.2, §4.4).

Outbound, Waddle calls exactly four KWin methods: ``loadScript``, ``isScriptLoaded``,
``unloadScript`` on ``/Scripting`` and ``run`` on the loaded script. Inbound, its scripts push
JSON strings to the ``DBUS_KWIN_INTERFACE`` object exported here. Nothing else on KWin is
touched, and the scripts only ever move Waddle's own windows.

Three kinds of script are loaded: ``waddle-core.js`` for as long as the link runs,
``waddle-cursor.js`` only between ``set_cursor_stream(True)`` and ``set_cursor_stream(False)``
(a running script cannot receive calls, so loading it is the switch), and generated one-shot
scripts that are written to a private file, run once, unloaded and deleted.

Any process on the session bus can call the exported methods, so every payload is parsed as
untrusted input and nothing received here is proof of what KWin said.

Threading: everything here runs on the I/O thread's asyncio loop, and **the callbacks are
invoked on that thread**. Callers forward them to the UI thread (queued Qt signals).
"""

# No ``from __future__ import annotations`` here: dbus-fast reads the method annotations at
# class creation and cannot resolve a stringified ``-> None``.

import asyncio
import contextlib
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

from dbus_fast import BusType, Message, MessageFlag, MessageType, NameFlag, RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusStr
from dbus_fast.service import ServiceInterface, dbus_method

from waddle_core.identity import APP_ID, DBUS_KWIN_INTERFACE, DBUS_PATH
from waddle_core.window import WindowInfo
from waddle_desktop import log
from waddle_desktop.kwin.scripts import (
    CORE_PLUGIN_NAME,
    CURSOR_PLUGIN_NAME,
    render_cursor_query_script,
    render_move_script,
    render_window_at_script,
)
from waddle_desktop.kwin.types import (
    CursorPos,
    OutputInfo,
    OwnWindow,
    PopupSet,
    WindowAt,
    parse_cursor,
    parse_desktop_changed,
    parse_hello,
    parse_move_done,
    parse_outputs,
    parse_own_window,
    parse_popups,
    parse_window_at,
    parse_window_info,
)

KWIN_SERVICE: Final = "org.kde.KWin"
SCRIPTING_PATH: Final = "/Scripting"
SCRIPTING_INTERFACE: Final = "org.kde.kwin.Scripting"
SCRIPT_INTERFACE: Final = "org.kde.kwin.Script"
CALL_TIMEOUT: Final = 2.0
RELOAD_DELAYS: Final = (0.0, 0.5, 2.0)  # KWin may own its name before /Scripting is ready

_BUS_SERVICE: Final = "org.freedesktop.DBus"
_BUS_PATH: Final = "/org/freedesktop/DBus"
_KWIN_OWNER_MATCH: Final = (
    f"type='signal',sender='{_BUS_SERVICE}',interface='{_BUS_SERVICE}',"
    f"member='NameOwnerChanged',arg0='{KWIN_SERVICE}'"
)


def _random_token() -> str:
    """The unguessable part of a one-shot script's file and plugin name."""
    return secrets.token_hex(8)


class _KWinError(Exception):
    """A KWin call failed; ``str()`` is a short reason that is safe to show and log."""


class _Receiver(ServiceInterface):
    """What the scripts call. Each method takes one JSON string and never raises."""

    def __init__(self, link: "KWinLink") -> None:
        super().__init__(DBUS_KWIN_INTERFACE)
        self._link = link

    @dbus_method()
    def Hello(self, payload: DBusStr) -> None:
        self._link._receive("Hello", payload)

    @dbus_method()
    def OwnWindow(self, payload: DBusStr) -> None:
        self._link._receive("OwnWindow", payload)

    @dbus_method()
    def Outputs(self, payload: DBusStr) -> None:
        self._link._receive("Outputs", payload)

    @dbus_method()
    def MoveDone(self, payload: DBusStr) -> None:
        self._link._receive("MoveDone", payload)

    @dbus_method()
    def Activated(self, payload: DBusStr) -> None:
        self._link._receive("Activated", payload)

    @dbus_method()
    def TargetChanged(self, payload: DBusStr) -> None:
        self._link._receive("TargetChanged", payload)

    @dbus_method()
    def Popups(self, payload: DBusStr) -> None:
        self._link._receive("Popups", payload)

    @dbus_method()
    def Cursor(self, payload: DBusStr) -> None:
        self._link._receive("Cursor", payload)

    @dbus_method()
    def DesktopChanged(self, payload: DBusStr) -> None:
        self._link._receive("DesktopChanged", payload)

    @dbus_method()
    def WindowAt(self, payload: DBusStr) -> None:
        self._link._receive("WindowAt", payload)


async def _session_bus() -> MessageBus:
    return await MessageBus(bus_type=BusType.SESSION).connect()


class KWinLink:
    def __init__(
        self,
        runtime_dir: Path,
        core_script: Path,
        *,
        on_own_window: Callable[[OwnWindow], None],
        on_outputs: Callable[[list[OutputInfo], str | None], None],
        on_status: Callable[[bool, str], None],
        on_activated: Callable[[WindowInfo], None] | None = None,
        on_target_changed: Callable[[WindowInfo], None] | None = None,
        on_popups: Callable[[PopupSet], None] | None = None,
        on_cursor: Callable[[CursorPos], None] | None = None,
        on_desktop_changed: Callable[[], None] | None = None,
        cursor_script: Path | None = None,
        bus_factory: Callable[[], Awaitable[MessageBus]] | None = None,
    ) -> None:
        """All callbacks run on the asyncio thread; one that raises is logged and ignored.

        ``on_activated`` gets every activation, Waddle's own windows included
        (``is_own``): the caller keeps the last one that is not its own as the target.
        ``on_target_changed`` gets that target's later states, ``on_popups`` its popups.
        ``on_cursor`` is only called while the cursor stream is switched on.
        """
        self._runtime_dir = runtime_dir
        self._core_script = core_script
        self._cursor_script = cursor_script
        self._on_own_window = on_own_window
        self._on_outputs = on_outputs
        self._on_status = on_status
        self._on_activated = on_activated
        self._on_target_changed = on_target_changed
        self._on_popups = on_popups
        self._on_cursor = on_cursor
        self._on_desktop_changed = on_desktop_changed
        self._bus_factory = bus_factory or _session_bus
        self._receiver = _Receiver(self)
        self._bus: MessageBus | None = None
        self._reload_task: asyncio.Task[None] | None = None
        self._move_lock = asyncio.Lock()
        self._pending_move: tuple[tuple[str, int, int], asyncio.Future[bool]] | None = None
        self._query_lock = asyncio.Lock()
        self._pending_cursor: asyncio.Future[CursorPos] | None = None
        self._pending_window_at: tuple[tuple[int, int], asyncio.Future[WindowAt]] | None = None
        self._cursor_lock = asyncio.Lock()
        self._cursor_wanted = False  # what set_cursor_stream last asked for
        self._cursor_loaded = False  # whether waddle-cursor.js is running in this KWin

    async def start(self) -> bool:
        """Connect, claim ``APP_ID``, export the receiver and load ``waddle-core.js``.

        Never raises. Returns False with ``on_status(False, reason)`` when something is
        missing. If only KWin is missing the link stays connected and loads the script when
        KWin appears; in the other cases it is left disconnected.
        """
        try:
            bus = await self._bus_factory()
        except Exception as exc:
            log.warning("kwin_link_no_bus", error=type(exc).__name__)
            self._status(False, "session bus unavailable")
            return False
        self._bus = bus
        try:
            bus.export(DBUS_PATH, self._receiver)
            if await bus.request_name(APP_ID, NameFlag.DO_NOT_QUEUE) not in (
                RequestNameReply.PRIMARY_OWNER,
                RequestNameReply.ALREADY_OWNER,
            ):
                await self._disconnect(release=False)
                self._status(False, "another Waddle is running")
                return False
            bus.add_message_handler(self._on_message)
            await self._call(
                _BUS_SERVICE, _BUS_PATH, _BUS_SERVICE, "AddMatch", "s", [_KWIN_OWNER_MATCH]
            )
        except Exception as exc:
            log.warning("kwin_link_setup_failed", error=type(exc).__name__)
            await self._disconnect(release=True)
            self._status(False, "session bus setup failed")
            return False
        loaded = await self._load_core()
        if loaded:
            await self._sync_cursor()  # the stream was asked for before start()
        return loaded

    async def move_own_window(self, title: str, x: int, y: int, timeout: float = 1.0) -> bool:
        """Move Waddle's own window titled ``title`` to logical (x, y) with a one-shot script.

        Returns True once the script confirms the move, False on timeout or any failure. The
        script file and the loaded script are removed in every case. Raises ``ValueError``
        for a title that is not Waddle's or coordinates out of range (a caller bug).
        """
        source = render_move_script(title, x, y)
        if self._bus is None:
            return False
        async with self._move_lock:
            done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._pending_move = ((title, x, y), done)
            try:
                return await self._one_shot("move", source, done, timeout) is True
            finally:
                self._pending_move = None

    async def query_cursor(self, timeout: float = 0.5) -> CursorPos | None:
        """Ask KWin once where the pointer is, with a one-shot script.

        Returns None on timeout or any failure. The script file and the loaded script are
        removed in every case. Works whether or not the cursor stream is on.
        """
        source = render_cursor_query_script()
        if self._bus is None:
            return None
        async with self._query_lock:
            done: asyncio.Future[CursorPos] = asyncio.get_running_loop().create_future()
            self._pending_cursor = done
            try:
                return await self._one_shot("query", source, done, timeout)
            finally:
                self._pending_cursor = None

    async def window_at(self, x: int, y: int, timeout: float = 0.5) -> WindowAt | None:
        """Ask KWin once which window is under logical (x, y), with a one-shot script.

        Returns ``WindowAt`` whose ``window`` is None when no application window is there,
        and None itself on timeout or any failure (not known). Raises ``ValueError`` for
        coordinates that are not ints in range (a caller bug). Cleans up like the others.
        """
        source = render_window_at_script(x, y)
        if self._bus is None:
            return None
        async with self._query_lock:
            done: asyncio.Future[WindowAt] = asyncio.get_running_loop().create_future()
            self._pending_window_at = ((x, y), done)
            try:
                return await self._one_shot("at", source, done, timeout)
            finally:
                self._pending_window_at = None

    async def set_cursor_stream(self, enabled: bool) -> bool:
        """Switch the pointer stream on or off by loading or unloading ``waddle-cursor.js``.

        Idempotent: asking for the state it is already in calls nothing. The wish is
        remembered, so the script is loaded again after a KWin restart, and loaded by
        ``start()`` if this was called first. Returns True when KWin is in the requested
        state on return, False when it could not be brought there (no cursor script, not
        connected, KWin not answering). Never raises.
        """
        self._cursor_wanted = bool(enabled)
        return await self._sync_cursor()

    async def stop(self) -> None:
        """Unload Waddle's scripts, release the name and disconnect. Never raises."""
        if self._reload_task is not None:
            self._reload_task.cancel()
            self._reload_task = None
        self._cursor_wanted = False
        if self._bus is None:
            self._cursor_loaded = False
            return
        await self._sync_cursor()
        self._cursor_loaded = False
        with contextlib.suppress(_KWinError):
            await self._scripting("unloadScript", CORE_PLUGIN_NAME)
        await self._disconnect(release=True)

    def _status(self, ok: bool, reason: str) -> None:
        try:
            self._on_status(ok, reason)
        except Exception as exc:  # a callback bug must not escape start() or kill a reload
            log.warning("kwin_status_callback_failed", error=type(exc).__name__)

    # --- inbound -------------------------------------------------------------------------

    def _receive(self, method: str, payload: str) -> None:
        try:
            if method == "OwnWindow":
                self._on_own_window(parse_own_window(payload))
            elif method == "Outputs":
                self._on_outputs(*parse_outputs(payload))
            elif method == "MoveDone":
                move = parse_move_done(payload)
                pending = self._pending_move
                if pending and pending[0] == (move.title, move.x, move.y) and not pending[1].done():
                    pending[1].set_result(move.ok)
            elif method == "Activated":
                window = parse_window_info(payload)
                if self._on_activated is not None:
                    self._on_activated(window)
            elif method == "TargetChanged":
                window = parse_window_info(payload)
                if self._on_target_changed is not None:
                    self._on_target_changed(window)
            elif method == "Popups":
                popups = parse_popups(payload)
                if self._on_popups is not None:
                    self._on_popups(popups)
            elif method == "Cursor":
                position = parse_cursor(payload)
                waiting = self._pending_cursor
                if waiting is not None and not waiting.done():
                    waiting.set_result(position)
                # A report that arrives after the stream was switched off is dropped.
                if self._cursor_wanted and self._on_cursor is not None:
                    self._on_cursor(position)
            elif method == "DesktopChanged":
                parse_desktop_changed(payload)
                if self._on_desktop_changed is not None:
                    self._on_desktop_changed()
            elif method == "WindowAt":
                found = parse_window_at(payload)
                asked = self._pending_window_at
                if asked and asked[0] == (found.x, found.y) and not asked[1].done():
                    asked[1].set_result(found)
            elif method == "Hello":
                hello = parse_hello(payload)
                log.event("kwin_hello", version=hello.version, api_ok=hello.api_ok)
                if hello.api_ok:
                    self._status(True, "KWin script running")
                else:
                    self._status(False, "KWin scripting API not as expected")
        except Exception as exc:
            # Type only: a payload may hold window titles, and a callback error must not
            # travel back to the calling script as a D-Bus error.
            log.warning("kwin_payload_rejected", method=method, error=type(exc).__name__)

    def _on_message(self, message: Message) -> None:
        if (
            message.message_type is MessageType.SIGNAL
            and message.member == "NameOwnerChanged"
            and message.interface == _BUS_SERVICE
            and message.sender == _BUS_SERVICE
            and len(message.body) == 3
            and message.body[0] == KWIN_SERVICE
        ):
            if self._reload_task is not None:
                self._reload_task.cancel()
                self._reload_task = None
            self._cursor_loaded = False  # a new KWin starts without Waddle's runtime scripts
            if message.body[2]:
                self._reload_task = asyncio.get_running_loop().create_task(self._reload_core())
            else:
                self._status(False, "KWin is not running")

    # --- outbound ------------------------------------------------------------------------

    async def _reload_core(self) -> None:
        reason: str | None = None
        for delay in RELOAD_DELAYS:
            await asyncio.sleep(delay)
            reason = await self._try_load_core()
            if reason is None:
                self._status(True, "KWin script reloaded")
                await self._sync_cursor()
                return
        self._status(False, reason or "KWin is not available")

    async def _load_core(self) -> bool:
        reason = await self._try_load_core()
        self._status(reason is None, reason or "KWin script loaded")
        return reason is None

    async def _try_load_core(self) -> str | None:
        """Load and run the core script; return None, or the reason it failed."""
        if not self._core_script.is_file():
            return "KWin script file is missing"
        try:
            if (await self._scripting("isScriptLoaded", CORE_PLUGIN_NAME))[:1] == [True]:
                await self._scripting("unloadScript", CORE_PLUGIN_NAME)  # stale, from a crash
            await self._load_and_run(self._core_script, CORE_PLUGIN_NAME)
        except _KWinError as exc:
            return str(exc)
        return None

    async def _sync_cursor(self) -> bool:
        """Bring KWin in line with ``_cursor_wanted``; True when it is on return."""
        async with self._cursor_lock:
            wanted = self._cursor_wanted
            if wanted == self._cursor_loaded:
                return True
            if self._bus is None:
                self._cursor_loaded = False
                return not wanted
            if not wanted:
                self._cursor_loaded = False  # if KWin is gone, so is the script
                try:
                    await self._scripting("unloadScript", CURSOR_PLUGIN_NAME)
                except _KWinError:
                    return False
                return True
            if self._cursor_script is None or not self._cursor_script.is_file():
                log.warning("kwin_cursor_script_missing")
                return False
            try:
                if (await self._scripting("isScriptLoaded", CURSOR_PLUGIN_NAME))[:1] == [True]:
                    await self._scripting("unloadScript", CURSOR_PLUGIN_NAME)  # stale
                await self._load_and_run(self._cursor_script, CURSOR_PLUGIN_NAME)
            except _KWinError:
                # Loaded but not started would report nothing and block the next load.
                with contextlib.suppress(_KWinError):
                    await self._scripting("unloadScript", CURSOR_PLUGIN_NAME)
                return False
            self._cursor_loaded = True
            return True

    async def _one_shot[T](
        self, kind: str, source: str, done: asyncio.Future[T], timeout: float
    ) -> T | None:
        """Write ``source`` to a private file, load and run it, and wait for ``done``.

        Returns None on timeout or failure. The plugin is always unloaded. The file is
        deleted if this call created it; a file or link that was already at the name makes
        the call fail and is left exactly as it was.
        """
        plugin = f"waddle-{kind}-{_random_token()}"
        path = self._runtime_dir / f"{plugin}.js"
        created = False
        try:
            async with asyncio.timeout(timeout):
                self._write_private(path, source)
                created = True
                await self._load_and_run(path, plugin)
                return await done
        except (TimeoutError, _KWinError, OSError) as exc:
            log.warning("kwin_one_shot_failed", kind=kind, error=type(exc).__name__)
            return None
        finally:
            if created:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                with contextlib.suppress(_KWinError):
                    await self._scripting("unloadScript", plugin)

    async def _load_and_run(self, script: Path, plugin: str) -> None:
        body = await self._scripting("loadScript", str(script), plugin)
        if len(body) != 1 or type(body[0]) is not int or body[0] < 0:
            raise _KWinError("KWin refused the script")
        await self._call(KWIN_SERVICE, f"{SCRIPTING_PATH}/Script{body[0]}", SCRIPT_INTERFACE, "run")

    async def _scripting(self, member: str, *args: str) -> list[object]:
        return await self._call(
            KWIN_SERVICE, SCRIPTING_PATH, SCRIPTING_INTERFACE, member, "s" * len(args), list(args)
        )

    async def _call(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: list[object] | None = None,
    ) -> list[object]:
        if self._bus is None:
            raise _KWinError("not connected")
        message = Message(
            destination=destination,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body or [],
            flags=MessageFlag.NO_AUTOSTART,
        )
        try:
            reply = await asyncio.wait_for(self._bus.call(message), CALL_TIMEOUT)
        except TimeoutError as exc:
            raise _KWinError("KWin did not answer") from exc
        except Exception as exc:
            log.warning("kwin_call_failed", member=member, error=type(exc).__name__)
            raise _KWinError("session bus error") from exc
        if reply is None or reply.message_type is not MessageType.METHOD_RETURN:
            raise _KWinError("KWin is not available")
        return list(reply.body)

    def _write_private(self, path: Path, text: str) -> None:
        self._runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._runtime_dir.chmod(0o700)
        # O_EXCL: never write into a file or through a link that something else put there.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            with contextlib.suppress(OSError):
                path.unlink()  # ours: it did not exist before the open above
            raise

    async def _disconnect(self, *, release: bool) -> None:
        bus, self._bus = self._bus, None
        if bus is None:
            return
        with contextlib.suppress(Exception):
            bus.remove_message_handler(self._on_message)
        with contextlib.suppress(Exception):
            bus.unexport(DBUS_PATH, self._receiver)
        if release:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(bus.release_name(APP_ID), CALL_TIMEOUT)
        with contextlib.suppress(Exception):
            bus.disconnect()
