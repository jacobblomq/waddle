"""The host <-> KWin link (ARCHITECTURE §4.2, §4.4).

Outbound, Waddle calls exactly four KWin methods: ``loadScript``, ``isScriptLoaded``,
``unloadScript`` on ``/Scripting`` and ``run`` on the loaded script. Inbound, its scripts push
JSON strings to the ``DBUS_KWIN_INTERFACE`` object exported here. Nothing else on KWin is
touched, and the scripts only ever move Waddle's own windows.

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
from waddle_desktop import log
from waddle_desktop.kwin.scripts import CORE_PLUGIN_NAME, render_move_script
from waddle_desktop.kwin.types import (
    OutputInfo,
    OwnWindow,
    parse_hello,
    parse_move_done,
    parse_outputs,
    parse_own_window,
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
        bus_factory: Callable[[], Awaitable[MessageBus]] | None = None,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._core_script = core_script
        self._on_own_window = on_own_window
        self._on_outputs = on_outputs
        self._on_status = on_status
        self._bus_factory = bus_factory or _session_bus
        self._receiver = _Receiver(self)
        self._bus: MessageBus | None = None
        self._reload_task: asyncio.Task[None] | None = None
        self._move_lock = asyncio.Lock()
        self._pending_move: tuple[tuple[str, int, int], asyncio.Future[bool]] | None = None

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
        return await self._load_core()

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
            plugin = f"waddle-move-{secrets.token_hex(8)}"
            path = self._runtime_dir / f"{plugin}.js"
            done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._pending_move = ((title, x, y), done)
            try:
                async with asyncio.timeout(timeout):
                    self._write_private(path, source)
                    await self._load_and_run(path, plugin)
                    return await done
            except (TimeoutError, _KWinError, OSError) as exc:
                log.warning("kwin_move_failed", error=type(exc).__name__)
                return False
            finally:
                self._pending_move = None
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                with contextlib.suppress(_KWinError):
                    await self._scripting("unloadScript", plugin)

    async def stop(self) -> None:
        """Unload the core script, release the name and disconnect. Never raises."""
        if self._reload_task is not None:
            self._reload_task.cancel()
            self._reload_task = None
        if self._bus is None:
            return
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
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)

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
