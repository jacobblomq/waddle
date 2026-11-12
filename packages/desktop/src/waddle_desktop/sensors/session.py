"""Session-state sensors: screen lock, do-not-disturb and idle (ARCHITECTURE §4.4).

``SessionWatcher`` listens on two buses and reports one ``SessionSnapshot`` whenever it
changes. Everything is pushed; nothing is polled.

Sources:

- **Lock, session bus:** ``org.freedesktop.ScreenSaver`` at ``/ScreenSaver`` and
  ``/org/freedesktop/ScreenSaver``: one ``GetActive`` at start, then ``ActiveChanged``.
- **Lock, system bus:** logind's session object: property ``LockedHint`` (read once, then
  ``PropertiesChanged``) and the ``Lock`` / ``Unlock`` signals.
- **Do-not-disturb:** ``org.freedesktop.Notifications`` property ``Inhibited`` (a KDE
  extension). Absent means "not in do-not-disturb".
- **Idle:** logind's ``IdleHint``. The screen saver's idle *time* is a poll and is not used;
  finer idle detection will come later from input-less heuristics.

Fail-safe rules (invariant I4):

- ``locked`` is the OR of every source that has answered; ``lock_known`` is True only while
  at least one has. The privacy gate treats "unknown" as blind.
- A logind ``Lock`` signal sets ``locked`` at once, before any property is read. The latch
  is dropped when a source reports the screen unlocked, on ``Unlock``, or after one delayed
  re-read (``lock_confirm_delay``), from which point the sources alone decide.
- A source whose service leaves the bus is forgotten immediately; if all lock sources are
  gone, ``lock_known`` turns False.
- A service that is not running is never started by us: values are only requested from
  names that currently have an owner.

Read-only: the module can call only the methods in ``METHODS`` (see ``CALLABLE_MEMBERS``),
all of which are queries, plus ``AddMatch`` on its own connections. Signals are accepted
only from the current owner of the service they claim to come from. Nothing received is
logged.

UNVERIFIED on a real Plasma 6.6 session (spike S2.5): which of these signals Plasma emits,
whether its locker sets ``LockedHint``, which screen-saver path it serves, whether
``Inhibited`` emits ``PropertiesChanged``, and whether ``IdleHint`` is ever set. Each source
is optional, so a missing one costs coverage, not correctness.

Threading: asyncio only. ``on_change`` runs on the event loop's thread.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Final

from dbus_fast import BusType, Message, MessageFlag, MessageType, Variant
from dbus_fast.aio import MessageBus

from waddle_desktop import log

__all__ = [
    "CALLABLE_MEMBERS",
    "METHODS",
    "SessionSnapshot",
    "SessionWatcher",
]

_BUS_NAME: Final = "org.freedesktop.DBus"
_BUS_PATH: Final = "/org/freedesktop/DBus"
_PROPERTIES: Final = "org.freedesktop.DBus.Properties"

SCREENSAVER_SERVICE: Final = "org.freedesktop.ScreenSaver"
SCREENSAVER_INTERFACE: Final = "org.freedesktop.ScreenSaver"
SCREENSAVER_PATHS: Final = ("/ScreenSaver", "/org/freedesktop/ScreenSaver")

NOTIFICATIONS_SERVICE: Final = "org.freedesktop.Notifications"
NOTIFICATIONS_INTERFACE: Final = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH: Final = "/org/freedesktop/Notifications"

LOGIN_SERVICE: Final = "org.freedesktop.login1"
LOGIN_PATH: Final = "/org/freedesktop/login1"
LOGIN_MANAGER_INTERFACE: Final = "org.freedesktop.login1.Manager"
LOGIN_SESSION_INTERFACE: Final = "org.freedesktop.login1.Session"
LOGIN_USER_INTERFACE: Final = "org.freedesktop.login1.User"
LOGIN_USER_SELF_PATH: Final = "/org/freedesktop/login1/user/self"
_LOGIN_SESSION_PREFIX: Final = "/org/freedesktop/login1/session/"

_SESSION_ID: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_OBJECT_PATH: Final = re.compile(r"^(/[A-Za-z0-9_]+)+$")
_MAX_PATH: Final = 255


@dataclass(frozen=True)
class _Method:
    """One D-Bus method this module may call."""

    interface: str
    member: str
    signature: str


_ADD_MATCH: Final = _Method(_BUS_NAME, "AddMatch", "s")
_GET_NAME_OWNER: Final = _Method(_BUS_NAME, "GetNameOwner", "s")
_GET_ACTIVE: Final = _Method(SCREENSAVER_INTERFACE, "GetActive", "")
_GET_SESSION: Final = _Method(LOGIN_MANAGER_INTERFACE, "GetSession", "s")
_GET_SESSION_BY_PID: Final = _Method(LOGIN_MANAGER_INTERFACE, "GetSessionByPID", "u")
_GET_PROPERTY: Final = _Method(_PROPERTIES, "Get", "ss")

METHODS: Final = (
    _ADD_MATCH,
    _GET_NAME_OWNER,
    _GET_ACTIVE,
    _GET_SESSION,
    _GET_SESSION_BY_PID,
    _GET_PROPERTY,
)
"""Every method call this module can make. ``_send`` refuses anything else."""

CALLABLE_MEMBERS: Final = frozenset(method.member for method in METHODS)

# Value slots, also used as keys for the "a signal overtook this read" counters.
_SS: Final = "screensaver"
_LOCKED: Final = "locked-hint"
_IDLE: Final = "idle-hint"
_DND: Final = "inhibited"


@dataclass(frozen=True)
class SessionSnapshot:
    """What the session sensors know right now.

    ``lock_known`` False means no lock source has answered: treat the screen as possibly
    locked. ``dnd`` and ``idle`` are False when their source is missing.
    """

    locked: bool
    lock_known: bool
    dnd: bool
    idle: bool


UNKNOWN: Final = SessionSnapshot(locked=False, lock_known=False, dnd=False, idle=False)
"""The snapshot before ``start`` and after ``stop``."""


@dataclass
class _Side:
    """One bus connection and the unique names of the services watched on it."""

    bus: MessageBus
    handler: Callable[[Message], bool]
    owners: dict[str, str | None] = field(default_factory=dict)
    rules: set[str] = field(default_factory=set)


async def _connect_session_bus() -> MessageBus:
    return await MessageBus(bus_type=BusType.SESSION).connect()


async def _connect_system_bus() -> MessageBus:
    return await MessageBus(bus_type=BusType.SYSTEM).connect()


def _variant_bool(value: object) -> bool | None:
    if isinstance(value, Variant) and value.signature == "b" and isinstance(value.value, bool):
        return value.value
    return None


class SessionWatcher:
    """Watches lock, do-not-disturb and idle; calls ``on_change`` when the snapshot changes.

    ``start`` never raises for a missing bus or service and returns the first snapshot,
    which is also delivered to ``on_change`` once. After that ``on_change`` is called only
    when the snapshot differs from the last one delivered. A callback that raises is logged
    and ignored. ``snapshot`` always holds the current value.
    """

    def __init__(
        self,
        session_bus_factory: Callable[[], Awaitable[MessageBus]] | None = None,
        system_bus_factory: Callable[[], Awaitable[MessageBus]] | None = None,
        *,
        on_change: Callable[[SessionSnapshot], None],
        session_id: str | None = None,
        pid: int | None = None,
        call_timeout: float = 2.0,
        lock_confirm_delay: float = 10.0,
    ) -> None:
        """Both factories are injected in tests (dbus-fast cannot fake the system bus, so
        tests pass a private bus for both). ``session_id`` defaults to ``XDG_SESSION_ID``
        and ``pid`` to this process; they only select which logind session to watch."""
        self._session_bus_factory = session_bus_factory or _connect_session_bus
        self._system_bus_factory = system_bus_factory or _connect_system_bus
        self._on_change = on_change
        self._session_id = (
            session_id if session_id is not None else os.environ.get("XDG_SESSION_ID", "")
        )
        self._pid = pid if pid is not None else os.getpid()
        self._call_timeout = call_timeout
        self._lock_confirm_delay = lock_confirm_delay
        self._session_side: _Side | None = None
        self._login_side: _Side | None = None
        self._login_path: str | None = None
        self._values: dict[str, bool | None] = dict.fromkeys((_SS, _LOCKED, _IDLE, _DND))
        self._epochs: dict[str, int] = {}
        self._latch = False  # a logind Lock signal that no source has confirmed or denied yet
        self._confirm_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._running = False
        self._ready = False
        self._delivered: SessionSnapshot | None = None

    # -- public ------------------------------------------------------------------------------

    @property
    def snapshot(self) -> SessionSnapshot:
        if not self._running:
            return UNKNOWN
        ss, hint = self._values[_SS], self._values[_LOCKED]
        return SessionSnapshot(
            locked=self._latch or ss is True or hint is True,
            lock_known=self._latch or ss is not None or hint is not None,
            dnd=self._values[_DND] is True,
            idle=self._values[_IDLE] is True,
        )

    async def start(self) -> SessionSnapshot:
        """Connect, subscribe, read the initial state once, and deliver the first snapshot."""
        if self._running:
            return self.snapshot
        self._running = True
        self._ready = False
        self._delivered = None
        await asyncio.gather(self._start_session_side(), self._start_login_side())
        self._ready = True
        self._publish()
        return self.snapshot

    async def stop(self) -> None:
        """Stop listening. No ``on_change`` call happens after this returns."""
        self._running = False
        self._ready = False
        self._latch = False
        tasks = [*self._tasks]
        self._tasks.clear()
        self._confirm_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for side in (self._session_side, self._login_side):
            if side is not None:
                side.bus.remove_message_handler(side.handler)
                side.bus.disconnect()
        self._session_side = self._login_side = None
        self._login_path = None
        self._values = dict.fromkeys((_SS, _LOCKED, _IDLE, _DND))

    # -- plumbing ----------------------------------------------------------------------------

    async def _send(
        self, bus: MessageBus, service: str, path: str, method: _Method, body: list[object]
    ) -> list[Any] | None:
        """One query. Returns the reply body, or ``None`` for an error, timeout or dead bus."""
        if method not in METHODS:
            raise AssertionError("method is not on the read-only list")
        message = Message(
            destination=service,
            path=path,
            interface=method.interface,
            member=method.member,
            signature=method.signature,
            body=body,
            # Belt and braces for "never started by us": the owner check before a query
            # can be overtaken by the service exiting.
            flags=MessageFlag.NO_AUTOSTART,
        )
        try:
            async with asyncio.timeout(self._call_timeout):
                reply = await bus.call(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # timeout, disconnect, marshalling: all mean "no answer"
            return None
        if reply is None or reply.message_type is not MessageType.METHOD_RETURN:
            return None
        return list(reply.body)

    async def _add_match(self, side: _Side, rule: str) -> None:
        if rule not in side.rules:
            side.rules.add(rule)
            await self._send(side.bus, _BUS_NAME, _BUS_PATH, _ADD_MATCH, [rule])

    async def _watch_owner(self, side: _Side, name: str) -> None:
        side.owners.setdefault(name, None)
        await self._add_match(
            side,
            f"type='signal',sender='{_BUS_NAME}',interface='{_BUS_NAME}',"
            f"member='NameOwnerChanged',arg0='{name}'",
        )

    async def _current_owner(self, side: _Side, name: str) -> str | None:
        """Ask who owns ``name`` (without starting it) and remember the answer."""
        epoch = self._epoch(name)
        body = await self._send(side.bus, _BUS_NAME, _BUS_PATH, _GET_NAME_OWNER, [name])
        owner = body[0] if body and isinstance(body[0], str) and body[0] else None
        if self._epoch(name) == epoch:
            side.owners[name] = owner
        return side.owners.get(name)

    async def _get_bool(
        self, side: _Side, service: str, path: str, interface: str, name: str
    ) -> bool | None:
        body = await self._send(side.bus, service, path, _GET_PROPERTY, [interface, name])
        return _variant_bool(body[0]) if body else None

    def _epoch(self, key: str) -> int:
        return self._epochs.get(key, 0)

    def _bump(self, *keys: str) -> None:
        for key in keys:
            self._epochs[key] = self._epoch(key) + 1

    def _apply(self, key: str, epoch: int, value: bool | None) -> None:
        """Store a value that was *read*, unless a signal for it arrived in the meantime
        (the signal is then at least as new as the reply)."""
        if self._running and self._epoch(key) == epoch:
            self._values[key] = value

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("session_task_failed", error=type(task.exception()).__name__)

    def _publish(self) -> None:
        if not self._running:
            return
        current = self.snapshot
        if not self._ready and not current.locked:
            return  # still reading the initial state; only "locked" may go out early
        if current == self._delivered:
            return
        self._delivered = current
        try:
            self._on_change(current)
        except Exception as exc:
            log.warning("session_callback_failed", error=type(exc).__name__)

    async def _connect(
        self,
        factory: Callable[[], Awaitable[MessageBus]],
        dispatch: Callable[[_Side, Message], None],
        which: str,
    ) -> _Side | None:
        try:
            bus = await factory()
        except Exception as exc:
            log.warning("session_no_bus", bus=which, error=type(exc).__name__)
            return None

        def handler(message: Message) -> bool:
            if self._running and message.message_type is MessageType.SIGNAL:
                try:
                    dispatch(side, message)
                except Exception as exc:  # a malformed signal must not take the handler down
                    log.warning("session_signal_error", bus=which, error=type(exc).__name__)
            return False

        side = _Side(bus, handler)
        bus.add_message_handler(handler)
        self._spawn(self._watch_disconnect(side, which))
        return side

    async def _watch_disconnect(self, side: _Side, which: str) -> None:
        try:
            await side.bus.wait_for_disconnect()  # type: ignore[no-untyped-call]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("session_bus_error", bus=which, error=type(exc).__name__)
        if not self._running:
            return
        log.warning("session_bus_lost", bus=which)
        for name in list(side.owners):
            self._owner_changed(side, name, "")

    # -- session bus: screen saver and notifications -----------------------------------------

    async def _start_session_side(self) -> None:
        side = await self._connect(self._session_bus_factory, self._dispatch_session, "session")
        if side is None:
            return
        self._session_side = side
        await self._watch_owner(side, SCREENSAVER_SERVICE)
        await self._watch_owner(side, NOTIFICATIONS_SERVICE)
        await self._add_match(
            side,
            f"type='signal',sender='{SCREENSAVER_SERVICE}',"
            f"interface='{SCREENSAVER_INTERFACE}',member='ActiveChanged'",
        )
        await self._add_match(
            side,
            f"type='signal',sender='{NOTIFICATIONS_SERVICE}',interface='{_PROPERTIES}',"
            f"member='PropertiesChanged',path='{NOTIFICATIONS_PATH}'",
        )
        await asyncio.gather(self._refresh_screensaver(), self._refresh_dnd())

    async def _refresh_screensaver(self) -> None:
        side = self._session_side
        if side is None:
            return
        epoch = self._epoch(_SS)
        answers: list[bool] = []
        if await self._current_owner(side, SCREENSAVER_SERVICE) is not None:
            replies = await asyncio.gather(
                *(
                    self._send(side.bus, SCREENSAVER_SERVICE, path, _GET_ACTIVE, [])
                    for path in SCREENSAVER_PATHS
                )
            )
            answers = [r[0] for r in replies if r and isinstance(r[0], bool)]
        self._apply(_SS, epoch, any(answers) if answers else None)
        self._publish()

    async def _refresh_dnd(self) -> None:
        side = self._session_side
        if side is None:
            return
        epoch = self._epoch(_DND)
        value: bool | None = None
        if await self._current_owner(side, NOTIFICATIONS_SERVICE) is not None:
            value = await self._get_bool(
                side, NOTIFICATIONS_SERVICE, NOTIFICATIONS_PATH, NOTIFICATIONS_INTERFACE,
                "Inhibited",
            )  # fmt: skip
        self._apply(_DND, epoch, value)
        self._publish()

    def _dispatch_session(self, side: _Side, message: Message) -> None:
        if self._name_owner_changed(side, message):
            return
        body = message.body
        if message.interface == SCREENSAVER_INTERFACE and message.member == "ActiveChanged":
            if message.signature != "b" or not self._from(side, SCREENSAVER_SERVICE, message):
                return
            self._bump(_SS)
            self._values[_SS] = bool(body[0])
            if not body[0]:
                self._latch = False
            self._publish()
        elif (
            message.interface == _PROPERTIES
            and message.member == "PropertiesChanged"
            and message.path == NOTIFICATIONS_PATH
        ):
            if (
                message.signature != "sa{sv}as"
                or body[0] != NOTIFICATIONS_INTERFACE
                or not self._from(side, NOTIFICATIONS_SERVICE, message)
            ):
                return
            value = _variant_bool(body[1].get("Inhibited"))
            if value is not None:
                self._bump(_DND)
                self._values[_DND] = value
                self._publish()
            elif "Inhibited" in body[2]:
                self._bump(_DND)
                self._spawn(self._refresh_dnd())

    # -- system bus: logind ------------------------------------------------------------------

    async def _start_login_side(self) -> None:
        side = await self._connect(self._system_bus_factory, self._dispatch_login, "system")
        if side is None:
            return
        self._login_side = side
        await self._watch_owner(side, LOGIN_SERVICE)
        await self._refresh_logind()

    async def _resolve_login_session(self, side: _Side) -> str | None:
        """Our logind session object: by id, else by pid, else the user's display session.

        UNVERIFIED (S2.5): which of these answers when Waddle runs as a systemd user
        service on the target (a user service is in no session scope, so only the third
        can work there).
        """
        candidates: list[object] = []
        if _SESSION_ID.match(self._session_id):
            body = await self._send(
                side.bus, LOGIN_SERVICE, LOGIN_PATH, _GET_SESSION, [self._session_id]
            )
            candidates.append(body[0] if body else None)
        if not any(self._is_session_path(c) for c in candidates):
            body = await self._send(
                side.bus, LOGIN_SERVICE, LOGIN_PATH, _GET_SESSION_BY_PID, [self._pid]
            )
            candidates.append(body[0] if body else None)
        if not any(self._is_session_path(c) for c in candidates):
            body = await self._send(
                side.bus, LOGIN_SERVICE, LOGIN_USER_SELF_PATH, _GET_PROPERTY,
                [LOGIN_USER_INTERFACE, "Display"],
            )  # fmt: skip
            display = body[0] if body else None
            if isinstance(display, Variant) and display.signature == "(so)":
                candidates.append(display.value[1])
        for candidate in candidates:
            if self._is_session_path(candidate):
                assert isinstance(candidate, str)
                return candidate
        return None

    @staticmethod
    def _is_session_path(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) <= _MAX_PATH
            and value.startswith(_LOGIN_SESSION_PREFIX)
            and _OBJECT_PATH.match(value) is not None
        )

    async def _refresh_logind(self) -> None:
        side = self._login_side
        if side is None:
            return
        locked_epoch, idle_epoch = self._epoch(_LOCKED), self._epoch(_IDLE)
        locked: bool | None = None
        idle: bool | None = None
        if await self._current_owner(side, LOGIN_SERVICE) is not None:
            if self._login_path is None:
                self._login_path = await self._resolve_login_session(side)
            path = self._login_path
            if path is not None:
                # Subscribe first, so nothing can change unseen between the read and the watch.
                await self._add_match(
                    side,
                    f"type='signal',sender='{LOGIN_SERVICE}',path='{path}',"
                    f"interface='{LOGIN_SESSION_INTERFACE}'",
                )
                await self._add_match(
                    side,
                    f"type='signal',sender='{LOGIN_SERVICE}',path='{path}',"
                    f"interface='{_PROPERTIES}',member='PropertiesChanged'",
                )
                locked, idle = await asyncio.gather(
                    self._get_bool(
                        side, LOGIN_SERVICE, path, LOGIN_SESSION_INTERFACE, "LockedHint"
                    ),
                    self._get_bool(side, LOGIN_SERVICE, path, LOGIN_SESSION_INTERFACE, "IdleHint"),
                )
        self._apply(_LOCKED, locked_epoch, locked)
        self._apply(_IDLE, idle_epoch, idle)
        self._publish()

    def _dispatch_login(self, side: _Side, message: Message) -> None:
        if self._name_owner_changed(side, message):
            return
        if message.interface == LOGIN_SESSION_INTERFACE and message.member == "Lock":
            # Fail-safe: go blind first, ask questions later. Accepted even while the owner
            # or our session path is still being resolved; a wrong Lock only costs a
            # moment of blindness, and the delayed re-read below corrects it.
            owner = side.owners.get(LOGIN_SERVICE)
            if owner is not None and message.sender != owner:
                return
            if self._login_path is not None and message.path != self._login_path:
                return
            if not self._is_session_path(message.path):
                return
            self._latch = True
            self._publish()
            if self._confirm_task is not None:
                self._confirm_task.cancel()
            self._confirm_task = self._spawn(self._confirm_lock())
            return
        if message.path != self._login_path or not self._from(side, LOGIN_SERVICE, message):
            return
        if message.interface == LOGIN_SESSION_INTERFACE and message.member == "Unlock":
            # A request to unlock: the hints say when it has really happened.
            self._latch = False
            self._publish()
            self._spawn(self._refresh_logind())
        elif message.interface == _PROPERTIES and message.member == "PropertiesChanged":
            body = message.body
            if message.signature != "sa{sv}as" or body[0] != LOGIN_SESSION_INTERFACE:
                return
            reread = False
            for key, name in ((_LOCKED, "LockedHint"), (_IDLE, "IdleHint")):
                value = _variant_bool(body[1].get(name))
                if value is not None:
                    self._bump(key)
                    self._values[key] = value
                    if key == _LOCKED and not value:
                        self._latch = False
                elif name in body[2]:
                    self._bump(key)
                    reread = True
            self._publish()
            if reread:
                self._spawn(self._refresh_logind())

    async def _confirm_lock(self) -> None:
        """Some time after a Lock signal, let the sources alone decide again."""
        await asyncio.sleep(self._lock_confirm_delay)
        await asyncio.gather(self._refresh_screensaver(), self._refresh_logind())
        self._latch = False
        self._publish()

    # -- services coming and going -----------------------------------------------------------

    def _from(self, side: _Side, service: str, message: Message) -> bool:
        owner = side.owners.get(service)
        return owner is not None and message.sender == owner

    def _name_owner_changed(self, side: _Side, message: Message) -> bool:
        if message.interface != _BUS_NAME or message.member != "NameOwnerChanged":
            return False
        body = message.body
        if message.sender == _BUS_NAME and message.signature == "sss" and body[0] in side.owners:
            self._owner_changed(side, body[0], body[2])
        return True

    def _owner_changed(self, side: _Side, name: str, new_owner: str) -> None:
        """A watched service left, arrived or was replaced: forget what it told us."""
        self._bump(name)
        side.owners[name] = new_owner or None
        if name == SCREENSAVER_SERVICE:
            keys: tuple[str, ...] = (_SS,)
            refresh: Callable[[], Coroutine[Any, Any, None]] = self._refresh_screensaver
        elif name == NOTIFICATIONS_SERVICE:
            keys, refresh = (_DND,), self._refresh_dnd
        else:
            keys, refresh = (_LOCKED, _IDLE), self._refresh_logind
            self._latch = False
            self._login_path = None
        self._bump(*keys)
        for key in keys:
            self._values[key] = None
        self._publish()
        if new_owner and side.bus.connected:
            self._spawn(refresh())
