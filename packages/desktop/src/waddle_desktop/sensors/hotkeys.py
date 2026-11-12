"""Global hotkeys (ARCHITECTURE §4.4 "Hotkeys", RESEARCH §4).

Three parts:

- ``find_conflicts`` / ``plan_bindings``: before anything is bound, ask KGlobalAccel which of
  the wanted keys are already taken. These are queries; nothing is registered or changed.
- ``PortalHotkeys``: registers Waddle's app id with the host portal registry, binds the
  actions through the GlobalShortcuts portal and turns ``Activated``/``Deactivated`` into
  ``on_pressed(action)``/``on_released(action)``. Both edges matter: the microphone is open
  only between them (invariant I7), so a release is also reported when the portal or the
  session goes away while a key is held.
- ``KGlobalAccelHotkeys``: the seam for a direct KGlobalAccel fallback. Not built yet.

The only things this module changes in the session are Waddle's own portal registration and
its own shortcut session (both on the list of sanctioned registrations, §1).

Everything that arrives over D-Bus is untrusted: types and lengths are checked, signals are
accepted only from the portal's current owner, and no received text is ever logged. Friendly
names of other applications' shortcuts are sanitized and capped for display.

Threading: asyncio only. Callbacks run on the event loop's thread; one that raises is logged
and ignored.

UNVERIFIED on a real Plasma 6.6 session (spike S2.4): the KGlobalAccel reply layout and match
constant, the portal registry's behaviour, the bind dialog, and ``Deactivated`` on key
release. Each assumption is marked where it is made.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from dbus_fast import BusType, Message, MessageFlag, MessageType, Variant
from dbus_fast.aio import MessageBus

from waddle_core.config import HotkeysConfig
from waddle_core.identity import APP_ID
from waddle_desktop import log
from waddle_desktop.sensors.keys import KeyCombo, alternatives, parse

__all__ = [
    "ACTIONS",
    "BindResult",
    "BindStatus",
    "BindingPlan",
    "Conflict",
    "KGlobalAccelHotkeys",
    "KGlobalAccelUnavailableError",
    "PortalHotkeys",
    "find_conflicts",
    "plan_bindings",
    "wanted_from_config",
]

ACTIONS: Final[dict[str, str]] = {
    "ask": "Ask Waddle a question",
    "push_to_talk": "Hold to talk to Waddle",
    "pause": "Pause or resume Waddle",
    "show_me_where": "Show me where",
    "region_select": "Ask about a region of the screen",
    "quiet": "Toggle quiet mode",
    "focus_mode": "Toggle Focus mode",
    "bring_back": "Bring Waddle back",
}
"""Action id -> description shown in System Settings. Ids are ``HotkeysConfig`` field names."""

MAX_FRIENDLY: Final = 64
"""Longest friendly name kept from another process."""

_BUS_NAME: Final = "org.freedesktop.DBus"
_BUS_PATH: Final = "/org/freedesktop/DBus"

_ERR_PREFIX: Final = "org.freedesktop.DBus.Error."
_ABSENT_ERRORS: Final = frozenset(
    {_ERR_PREFIX + "ServiceUnknown", _ERR_PREFIX + "NameHasNoOwner", _ERR_PREFIX + "Disconnected"}
)
_NO_SUCH_API_ERRORS: Final = frozenset(
    {_ERR_PREFIX + "UnknownMethod", _ERR_PREFIX + "UnknownInterface", _ERR_PREFIX + "UnknownObject"}
)


class _CallError(Exception):
    """A D-Bus call came back as an error, timed out, or could not be sent."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    @property
    def absent(self) -> bool:
        return self.name in _ABSENT_ERRORS or self.name.startswith(_ERR_PREFIX + "Spawn.")

    @property
    def no_such_api(self) -> bool:
        return self.name in _NO_SUCH_API_ERRORS


_TIMEOUT_ERROR: Final = "waddle.Timeout"
_SEND_ERROR: Final = "waddle.SendFailed"
_SAFE_ERROR: Final = re.compile(r"^[A-Za-z0-9_.]{1,64}$")


def _error_tag(exc: BaseException) -> str:
    """A short, log-safe name for a failure (an error *name*, never its message)."""
    if isinstance(exc, _CallError):
        return exc.name if _SAFE_ERROR.match(exc.name) else "error"
    return type(exc).__name__


async def _call(
    bus: MessageBus,
    destination: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: list[object] | None = None,
    *,
    timeout: float,
    autostart: bool = True,
) -> Message:
    """One method call. Returns the reply; raises ``_CallError`` for every failure.

    With ``autostart=False`` the bus is told not to start the service for this call: a
    service that is not running answers ``ServiceUnknown`` instead.
    """
    message = Message(
        destination=destination,
        path=path,
        interface=interface,
        member=member,
        signature=signature,
        body=body or [],
        flags=MessageFlag.NONE if autostart else MessageFlag.NO_AUTOSTART,
    )
    try:
        async with asyncio.timeout(timeout):
            reply = await bus.call(message)
    except TimeoutError:
        raise _CallError(_TIMEOUT_ERROR) from None
    except Exception as exc:
        raise _CallError(_SEND_ERROR) from exc
    if reply is None:
        raise _CallError(_SEND_ERROR)
    if reply.message_type is MessageType.ERROR:
        raise _CallError(reply.error_name or "error")
    return reply


def sanitize_friendly(value: object) -> str:
    """Make a name from another process fit for one line of UI: printable, short, or empty."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value[: MAX_FRIENDLY * 4])
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_FRIENDLY:
        cleaned = cleaned[: MAX_FRIENDLY - 1].rstrip() + "…"
    return cleaned


# --------------------------------------------------------------------------------------------
# Conflict detection (read-only queries to KGlobalAccel)
# --------------------------------------------------------------------------------------------

KGLOBALACCEL_SERVICE: Final = "org.kde.kglobalaccel"
KGLOBALACCEL_PATH: Final = "/kglobalaccel"
KGLOBALACCEL_INTERFACE: Final = "org.kde.KGlobalAccel"

# UNVERIFIED (S2.4), taken from kglobalacceld's interface description, not from a live bus:
# a QKeySequence travels as a struct holding an array of four ints (unused slots are 0), the
# match type "Equal" is 0, and each returned KGlobalShortcutInfo starts with four strings:
# action unique name, action friendly name, component unique name, component friendly name.
_BY_KEY_MEMBER: Final = "globalShortcutsByKey"
_BY_KEY_IN_SIGNATURE: Final = "(ai)i"
_BY_KEY_OUT_SIGNATURE: Final = "a(ssssssaiai)"
_MATCH_EQUAL: Final = 0
_AVAILABLE_MEMBER: Final = "globalShortcutAvailable"
_AVAILABLE_IN_SIGNATURE: Final = "(ai)s"
_KEY_SEQUENCE_SLOTS: Final = 4
_INFO_ACTION_FRIENDLY: Final = 1
_INFO_COMPONENT_UNIQUE: Final = 2
_INFO_COMPONENT_FRIENDLY: Final = 3
_MAX_INFOS: Final = 64

QUERY_MEMBERS: Final = frozenset({_BY_KEY_MEMBER, _AVAILABLE_MEMBER})
"""Every KGlobalAccel method this module can call. Both only answer questions."""


class KGlobalAccelUnavailableError(RuntimeError):
    """Neither conflict query could be answered, so nothing is known about conflicts."""


@dataclass(frozen=True)
class Conflict:
    """Who already holds a key. Both names are sanitized display text; either may be empty
    (the fallback query only says *that* a key is taken, not by whom)."""

    component_friendly: str
    action_friendly: str


@dataclass(frozen=True)
class BindingPlan:
    """What to do about one action.

    ``combo`` is the key to ask the portal for, or ``None`` when the wanted key is taken; then
    ``conflict`` says by whom and ``alternatives`` lists candidates to offer instead.
    """

    action: str
    combo: KeyCombo | None
    alternatives: tuple[KeyCombo, ...]
    conflict: Conflict | None
    wanted: KeyCombo


def _key_sequence(combo: KeyCombo) -> list[object]:
    """``combo`` as the D-Bus struct ``(ai)``: one array of four key slots."""
    return [[combo.to_qt_int()] + [0] * (_KEY_SEQUENCE_SLOTS - 1)]


async def _shortcuts_by_key(
    bus: MessageBus, combo: KeyCombo, timeout: float
) -> list[tuple[str, Conflict]]:
    """Ask who holds ``combo``: ``[(component unique name, Conflict)]``. Raises ``_CallError``
    when the method is missing or its reply does not have the expected layout (S2.4)."""
    reply = await _call(
        bus,
        KGLOBALACCEL_SERVICE,
        KGLOBALACCEL_PATH,
        KGLOBALACCEL_INTERFACE,
        _BY_KEY_MEMBER,
        _BY_KEY_IN_SIGNATURE,
        [_key_sequence(combo), _MATCH_EQUAL],
        timeout=timeout,
        autostart=False,  # asking must not start kglobalacceld where it is not running
    )
    if reply.signature != _BY_KEY_OUT_SIGNATURE or len(reply.body) != 1:
        raise _CallError("waddle.UnexpectedReply")
    holders: list[tuple[str, Conflict]] = []
    for info in reply.body[0][:_MAX_INFOS]:
        component = info[_INFO_COMPONENT_UNIQUE]
        holders.append(
            (
                component if isinstance(component, str) else "",
                Conflict(
                    sanitize_friendly(info[_INFO_COMPONENT_FRIENDLY]),
                    sanitize_friendly(info[_INFO_ACTION_FRIENDLY]),
                ),
            )
        )
    return holders


async def _shortcut_available(bus: MessageBus, combo: KeyCombo, own: str, timeout: float) -> bool:
    """The older query: is ``combo`` free for component ``own``? Raises ``_CallError``."""
    reply = await _call(
        bus,
        KGLOBALACCEL_SERVICE,
        KGLOBALACCEL_PATH,
        KGLOBALACCEL_INTERFACE,
        _AVAILABLE_MEMBER,
        _AVAILABLE_IN_SIGNATURE,
        [_key_sequence(combo), own],
        timeout=timeout,
        autostart=False,
    )
    if reply.signature != "b":
        raise _CallError("waddle.UnexpectedReply")
    return bool(reply.body[0])


async def find_conflicts(
    bus: MessageBus,
    wanted: Mapping[str, KeyCombo],
    *,
    own_components: tuple[str, ...] = (APP_ID,),
    timeout: float = 2.0,
) -> dict[str, Conflict]:
    """Which of the ``wanted`` keys (action id -> combo) another component already holds.

    Read-only. A key held by one of ``own_components`` (Waddle's own registration from an
    earlier run) is not a conflict. Uses ``globalShortcutsByKey`` and, if that fails, the
    older ``globalShortcutAvailable``, which cannot name the holder. Raises
    ``KGlobalAccelUnavailableError`` if neither answers: "no conflicts" must not be
    mistaken for "could not ask".
    """
    own = {name.casefold() for name in own_components}
    primary_own = own_components[0] if own_components else APP_ID
    by_key_works = True
    conflicts: dict[str, Conflict] = {}
    for action, combo in wanted.items():
        if by_key_works:
            try:
                holders = await _shortcuts_by_key(bus, combo, timeout)
            except _CallError as exc:
                log.event("hotkeys_by_key_failed", error=_error_tag(exc))
                by_key_works = False
            else:
                foreign = [c for component, c in holders if component.casefold() not in own]
                if foreign:
                    conflicts[action] = foreign[0]
                continue
        try:
            if not await _shortcut_available(bus, combo, primary_own, timeout):
                conflicts[action] = Conflict("", "")
        except _CallError as exc:
            log.warning("hotkeys_conflict_check_failed", error=_error_tag(exc))
            raise KGlobalAccelUnavailableError(_error_tag(exc)) from None
    return conflicts


def wanted_from_config(config: HotkeysConfig) -> tuple[dict[str, KeyCombo], dict[str, str]]:
    """Parse every configured shortcut: ``(action -> combo, action -> why it is invalid)``.

    Both dicts are in ``ACTIONS`` order. The reasons come from ``keys.parse`` and never
    repeat the configured text.
    """
    combos: dict[str, KeyCombo] = {}
    invalid: dict[str, str] = {}
    for action in ACTIONS:
        try:
            combos[action] = parse(getattr(config, action))
        except ValueError as exc:
            invalid[action] = str(exc)
    return combos, invalid


def plan_bindings(
    wanted: Mapping[str, KeyCombo], conflicts: Mapping[str, Conflict]
) -> list[BindingPlan]:
    """Pure: one plan per wanted action, in ``ACTIONS`` order.

    An action whose key is in ``conflicts`` gets ``combo=None`` plus alternatives. So does an
    action that wants the same key as an earlier Waddle action (the conflict then names
    Waddle and the earlier action). Alternatives never repeat a key another action wants.
    Raises ``ValueError`` for an action id that is not in ``ACTIONS``.
    """
    unknown = sorted(set(wanted) - set(ACTIONS))
    if unknown:
        raise ValueError(f"unknown hotkey actions: {', '.join(unknown)}")
    all_wanted = set(wanted.values())
    first_user: dict[KeyCombo, str] = {}
    plans: list[BindingPlan] = []
    for action in ACTIONS:
        if action not in wanted:
            continue
        combo = wanted[action]
        conflict = conflicts.get(action)
        if conflict is None and combo in first_user:
            conflict = Conflict("Waddle", ACTIONS[first_user[combo]])
        if conflict is None:
            first_user[combo] = action
            plans.append(BindingPlan(action, combo, (), None, combo))
        else:
            options = tuple(alternatives(combo, taken=all_wanted))
            plans.append(BindingPlan(action, None, options, conflict, combo))
    return plans


# --------------------------------------------------------------------------------------------
# The GlobalShortcuts portal
# --------------------------------------------------------------------------------------------

PORTAL_SERVICE: Final = "org.freedesktop.portal.Desktop"
PORTAL_PATH: Final = "/org/freedesktop/portal/desktop"
REGISTRY_INTERFACE: Final = "org.freedesktop.host.portal.Registry"
SHORTCUTS_INTERFACE: Final = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_INTERFACE: Final = "org.freedesktop.portal.Request"
SESSION_INTERFACE: Final = "org.freedesktop.portal.Session"
_REQUEST_PREFIX: Final = PORTAL_PATH + "/request"
_SESSION_PREFIX: Final = PORTAL_PATH + "/session"
_OBJECT_PATH: Final = re.compile(r"^(/[A-Za-z0-9_]+)+$")
_MAX_PATH: Final = 255
_MAX_SHORTCUTS: Final = 64


class BindStatus(StrEnum):
    BOUND = "bound"  # the session exists and is listening (``bound`` says for which actions)
    PORTAL_ABSENT = "portal-absent"  # no portal, or it has no GlobalShortcuts interface
    CANCELLED = "cancelled"  # the user dismissed the dialog, or the portal refused
    TIMEOUT = "timeout"  # no answer in time
    FAILED = "failed"  # the bus is unavailable or the portal answered nonsense


@dataclass(frozen=True)
class BindResult:
    """Outcome of ``PortalHotkeys.start``. Anything but ``BOUND`` means: fall back.

    ``bound`` are the action ids the portal reports as bound, ``missing`` the requested ones
    it does not. ``triggers`` maps a bound action to the portal's description of its key
    (sanitized display text; may be absent).
    """

    status: BindStatus
    bound: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    triggers: Mapping[str, str] | None = None

    @property
    def ok(self) -> bool:
        return self.status is BindStatus.BOUND


def _token() -> str:
    return "waddle_" + secrets.token_hex(8)


def sender_token(unique_name: str) -> str:
    """The caller part of a portal request path: ``:1.42`` -> ``1_42``."""
    return unique_name.removeprefix(":").replace(".", "_")


def _valid_path(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _MAX_PATH
        and value.startswith(prefix + "/")
        and _OBJECT_PATH.match(value) is not None
    )


async def _connect_session_bus() -> MessageBus:
    return await MessageBus(bus_type=BusType.SESSION).connect()


class _Outcome(Exception):
    """Internal: abandon ``start`` with this status."""

    def __init__(self, status: BindStatus) -> None:
        super().__init__(status.value)
        self.status = status


def _status_for(exc: _CallError) -> BindStatus:
    if exc.absent or exc.no_such_api:
        return BindStatus.PORTAL_ABSENT
    if exc.name == _TIMEOUT_ERROR:
        return BindStatus.TIMEOUT
    return BindStatus.FAILED


class PortalHotkeys:
    """Global shortcuts through ``org.freedesktop.portal.GlobalShortcuts``.

    ``await start({action: combo or None})`` returns a ``BindResult``; it never raises for
    anything the environment does. While bound, ``on_pressed(action)`` and
    ``on_released(action)`` alternate strictly per action: a repeated ``Activated`` without a
    ``Deactivated`` is ignored, and so is a ``Deactivated`` for a key that is not down.
    If the portal goes away or closes the session, every held key is released first and
    ``on_lost()`` is called once; the caller may then ``start`` again or fall back.
    """

    def __init__(
        self,
        bus_factory: Callable[[], Awaitable[MessageBus]] | None = None,
        *,
        on_pressed: Callable[[str], None],
        on_released: Callable[[str], None],
        on_lost: Callable[[], None] | None = None,
        app_id: str = APP_ID,
        call_timeout: float = 5.0,
        bind_timeout: float = 120.0,
        close_bus: bool = True,
    ) -> None:
        """``bus_factory`` connects to the session bus (injected in tests). The portal
        identifies an app by its connection, so the registration and all later calls use this
        one connection. ``bind_timeout`` covers the time the user may spend in the portal's
        dialog. With ``close_bus=False`` the connection is left open on ``stop`` (for a
        factory that hands out a shared connection)."""
        self._bus_factory = bus_factory or _connect_session_bus
        self._on_pressed = on_pressed
        self._on_released = on_released
        self._on_lost = on_lost
        self._app_id = app_id
        self._call_timeout = call_timeout
        self._bind_timeout = bind_timeout
        self._close_bus = close_bus
        self._bus: MessageBus | None = None
        self._owner: str | None = None  # unique name of the portal; signals must come from it
        self._session: str | None = None
        self._wanted: frozenset[str] = frozenset()
        self._pressed: set[str] = set()
        self._pending: dict[str, asyncio.Future[tuple[int, dict[str, Variant]]]] = {}
        self._rules: list[str] = []
        self._bus_watch: asyncio.Task[None] | None = None
        self._active = False
        self._start_lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        """True between a ``BOUND`` start and ``stop`` or loss of the portal."""
        return self._active

    @property
    def pressed(self) -> frozenset[str]:
        """Actions whose key is held right now."""
        return frozenset(self._pressed)

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self, bindings: Mapping[str, KeyCombo | None]) -> BindResult:
        """Register, create a session and bind ``bindings`` (action id -> preferred key).

        ``None`` binds an action with no preferred key, leaving the choice to the user in the
        portal's dialog. Raises ``ValueError`` for an unknown action id or an empty mapping
        (caller bugs); every runtime failure is a ``BindResult`` and leaves nothing behind.
        """
        unknown = sorted(set(bindings) - set(ACTIONS))
        if unknown:
            raise ValueError(f"unknown hotkey actions: {', '.join(unknown)}")
        if not bindings:
            raise ValueError("nothing to bind")
        async with self._start_lock:
            if self._bus is not None:
                await self._teardown(close_session=True)
            try:
                result = await self._start(bindings)
            except _Outcome as outcome:
                result = BindResult(
                    outcome.status, missing=tuple(a for a in ACTIONS if a in bindings)
                )
            except _CallError as exc:
                log.warning("hotkeys_portal_call_failed", error=_error_tag(exc))
                result = BindResult(
                    _status_for(exc), missing=tuple(a for a in ACTIONS if a in bindings)
                )
            if not result.ok:
                await self._teardown(close_session=True)
            log.event("hotkeys_portal_start", status=result.status.value, bound=len(result.bound))
            return result

    async def _start(self, bindings: Mapping[str, KeyCombo | None]) -> BindResult:
        try:
            bus = await self._bus_factory()
        except Exception as exc:
            log.warning("hotkeys_no_bus", error=type(exc).__name__)
            raise _Outcome(BindStatus.FAILED) from None
        self._bus = bus
        unique = bus.unique_name
        if not unique:
            raise _Outcome(BindStatus.FAILED)
        sender = sender_token(unique)
        bus.add_message_handler(self._on_message)
        self._bus_watch = asyncio.get_running_loop().create_task(self._watch_bus(bus))

        # Subscribe before any call, so a Response sent before the method returns is not lost.
        await self._add_match(
            f"type='signal',sender='{_BUS_NAME}',interface='{_BUS_NAME}',"
            f"member='NameOwnerChanged',arg0='{PORTAL_SERVICE}'"
        )
        await self._add_match(
            f"type='signal',sender='{PORTAL_SERVICE}',interface='{REQUEST_INTERFACE}',"
            f"member='Response',path_namespace='{_REQUEST_PREFIX}/{sender}'"
        )
        await self._add_match(
            f"type='signal',sender='{PORTAL_SERVICE}',interface='{SHORTCUTS_INTERFACE}',"
            f"path='{PORTAL_PATH}'"
        )
        await self._add_match(
            f"type='signal',sender='{PORTAL_SERVICE}',interface='{SESSION_INTERFACE}',"
            f"member='Closed',path_namespace='{_SESSION_PREFIX}/{sender}'"
        )

        await self._register()
        owner = await self._call_bus("GetNameOwner", "s", [PORTAL_SERVICE])
        if not (owner.signature == "s" and isinstance(owner.body[0], str) and owner.body[0]):
            raise _Outcome(BindStatus.PORTAL_ABSENT)
        self._owner = owner.body[0]

        session_token = _token()
        code, results = await self._request(
            "CreateSession",
            "a{sv}",
            lambda token: [
                {
                    "handle_token": Variant("s", token),
                    "session_handle_token": Variant("s", session_token),
                }
            ],
            self._call_timeout,
        )
        if code != 0:
            raise _Outcome(BindStatus.CANCELLED)
        handle = results.get("session_handle")
        session = handle.value if isinstance(handle, Variant) else None
        if not _valid_path(session, f"{_SESSION_PREFIX}/{sender}"):
            # A session path that is not ours would make us listen to someone else's keys.
            raise _Outcome(BindStatus.FAILED)
        assert isinstance(session, str)
        self._session = session
        self._wanted = frozenset(bindings)

        shortcuts: list[list[object]] = []
        for action in ACTIONS:
            if action not in bindings:
                continue
            options = {"description": Variant("s", ACTIONS[action])}
            combo = bindings[action]
            if combo is not None:
                options["preferred_trigger"] = Variant("s", combo.to_portal())
            shortcuts.append([action, options])
        # UNVERIFIED (S2.4): on Plasma this opens a dialog the first time and binds silently
        # afterwards; hence the long timeout.
        code, results = await self._request(
            "BindShortcuts",
            "oa(sa{sv})sa{sv}",
            lambda token: [session, shortcuts, "", {"handle_token": Variant("s", token)}],
            self._bind_timeout,
        )
        if code != 0:
            raise _Outcome(BindStatus.CANCELLED)
        triggers = _parse_shortcuts(results.get("shortcuts"), self._wanted)
        try:
            listed = await self.list_shortcuts()
        except _CallError as exc:  # the bind answer stands if the list cannot be read
            log.event("hotkeys_list_failed", error=_error_tag(exc))
        else:
            if listed is not None:
                triggers = listed
        if self._session != session:  # the portal went away while we were binding
            raise _Outcome(BindStatus.FAILED)
        self._active = True
        return BindResult(
            BindStatus.BOUND,
            bound=tuple(a for a in ACTIONS if a in triggers),
            missing=tuple(a for a in ACTIONS if a in bindings and a not in triggers),
            triggers=triggers,
        )

    async def list_shortcuts(self) -> dict[str, str] | None:
        """Ask the portal what this session has bound: action id -> trigger description.

        ``None`` if there is no session or the portal declined. Raises nothing the caller
        has to handle outside this module except through ``start``.
        """
        session = self._session
        if session is None:
            return None
        code, results = await self._request(
            "ListShortcuts",
            "oa{sv}",
            lambda token: [session, {"handle_token": Variant("s", token)}],
            self._call_timeout,
        )
        if code != 0:
            return None
        return _parse_shortcuts(results.get("shortcuts"), self._wanted)

    async def stop(self) -> None:
        """Release held keys (``on_released``), close our portal session, drop the bus."""
        async with self._start_lock:
            self._release_all()
            await self._teardown(close_session=True)

    async def _teardown(self, *, close_session: bool) -> None:
        bus, session = self._bus, self._session
        self._active = False
        self._session = None
        self._owner = None
        self._wanted = frozenset()
        self._bus = None
        watch, self._bus_watch = self._bus_watch, None
        if watch is not None:
            watch.cancel()  # we are about to drop the connection ourselves: not a loss
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        if bus is None:
            return
        if close_session and session is not None and bus.connected:
            try:  # our own session object; closing it unbinds nothing but our listening
                await _call(bus, PORTAL_SERVICE, session, SESSION_INTERFACE, "Close", timeout=1.0)
            except _CallError as exc:
                log.event("hotkeys_session_close_failed", error=_error_tag(exc))
        bus.remove_message_handler(self._on_message)
        rules, self._rules = self._rules, []
        if self._close_bus:
            bus.disconnect()
        elif bus.connected:
            for rule in rules:
                try:
                    await _call(
                        bus, _BUS_NAME, _BUS_PATH, _BUS_NAME, "RemoveMatch", "s", [rule],
                        timeout=1.0,
                    )  # fmt: skip
                except _CallError:
                    break

    async def _watch_bus(self, bus: MessageBus) -> None:
        """A dead connection delivers neither ``Deactivated`` nor ``NameOwnerChanged``, so
        losing it is treated like losing the portal: held keys are released (I7)."""
        try:
            # Shielded: cancelling this watch must not cancel the connection's own future,
            # which a shared connection (``close_bus=False``) has other waiters on.
            await asyncio.shield(bus.wait_for_disconnect())  # type: ignore[no-untyped-call]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("hotkeys_bus_error", error=type(exc).__name__)
        if self._bus is bus:
            self._lost("bus_lost")

    # -- calls -------------------------------------------------------------------------------

    def _require_bus(self) -> MessageBus:
        if self._bus is None:
            raise _CallError(_SEND_ERROR)
        return self._bus

    async def _call_bus(self, member: str, signature: str, body: list[object]) -> Message:
        return await _call(
            self._require_bus(), _BUS_NAME, _BUS_PATH, _BUS_NAME, member, signature, body,
            timeout=self._call_timeout,
        )  # fmt: skip

    async def _add_match(self, rule: str) -> None:
        await self._call_bus("AddMatch", "s", [rule])
        self._rules.append(rule)

    async def _register(self) -> None:
        """Tell the portal which app this connection is. Must precede every other call.

        UNVERIFIED (S2.4): the error a second registration gets, and whether the registry
        exists on the target's xdg-desktop-portal. Both are tolerated: only a missing portal
        *service* stops the flow here.
        """
        try:
            await _call(
                self._require_bus(), PORTAL_SERVICE, PORTAL_PATH, REGISTRY_INTERFACE, "Register",
                "sa{sv}", [self._app_id, {}], timeout=self._call_timeout,
            )  # fmt: skip
        except _CallError as exc:
            if exc.absent:
                raise _Outcome(BindStatus.PORTAL_ABSENT) from None
            if exc.name == _TIMEOUT_ERROR:
                raise _Outcome(BindStatus.TIMEOUT) from None
            log.event("hotkeys_registry_declined", error=_error_tag(exc))

    async def _request(
        self,
        member: str,
        signature: str,
        build: Callable[[str], list[object]],
        timeout: float,
    ) -> tuple[int, dict[str, Variant]]:
        """Call a portal method that answers through a Request object; return its Response.

        The Response is awaited on the path the portal is specified to use for our token, and
        that wait is in place before the call goes out.
        """
        bus = self._require_bus()
        token = _token()
        predicted = f"{_REQUEST_PREFIX}/{sender_token(bus.unique_name or '')}/{token}"
        response: asyncio.Future[tuple[int, dict[str, Variant]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[predicted] = response
        actual: str | None = None
        try:
            try:
                async with asyncio.timeout(timeout):
                    reply = await _call(
                        bus, PORTAL_SERVICE, PORTAL_PATH, SHORTCUTS_INTERFACE, member,
                        signature, build(token), timeout=self._call_timeout,
                    )  # fmt: skip
                    handle = reply.body[0] if reply.signature == "o" else None
                    if handle != predicted:
                        # Only very old portals pick their own path; then the race is theirs.
                        if not _valid_path(handle, _REQUEST_PREFIX):
                            raise _CallError("waddle.UnexpectedReply")
                        assert isinstance(handle, str)
                        actual = handle
                        self._pending[actual] = response
                        await self._add_match(
                            f"type='signal',sender='{PORTAL_SERVICE}',"
                            f"interface='{REQUEST_INTERFACE}',member='Response',path='{actual}'"
                        )
                    return await response
            except TimeoutError:
                await self._close_request(actual or predicted)
                raise _CallError(_TIMEOUT_ERROR) from None
            except asyncio.CancelledError:
                if response.cancelled():  # torn down under us: the portal or the bus is gone
                    raise _CallError(_SEND_ERROR) from None
                raise
        finally:
            self._pending.pop(predicted, None)
            if actual is not None:
                self._pending.pop(actual, None)

    async def _close_request(self, path: str) -> None:
        """Withdraw our own unanswered request, so its dialog does not linger."""
        bus = self._bus
        if bus is None or not bus.connected:
            return
        try:
            await _call(bus, PORTAL_SERVICE, path, REQUEST_INTERFACE, "Close", timeout=1.0)
        except _CallError as exc:
            log.event("hotkeys_request_close_failed", error=_error_tag(exc))

    # -- signals -----------------------------------------------------------------------------

    def _on_message(self, message: Message) -> bool:
        if message.message_type is not MessageType.SIGNAL:
            return False
        try:
            self._dispatch(message)
        except Exception as exc:  # a malformed signal must never take the handler down
            log.warning("hotkeys_signal_error", error=type(exc).__name__)
        return False

    def _dispatch(self, message: Message) -> None:
        interface, member, body = message.interface, message.member, message.body
        if interface == _BUS_NAME and member == "NameOwnerChanged":
            if (
                message.sender == _BUS_NAME
                and message.signature == "sss"
                and body[0] == PORTAL_SERVICE
            ):
                self._owner_changed(body[2])
            return
        if self._owner is None or message.sender != self._owner:
            return  # not from the portal: ignore, whatever it claims to be
        if interface == REQUEST_INTERFACE and member == "Response":
            future = self._pending.get(message.path or "")
            if future is None or future.done() or message.signature != "ua{sv}":
                return
            code, results = body
            future.set_result((int(code), results if isinstance(results, dict) else {}))
        elif interface == SHORTCUTS_INTERFACE and member in ("Activated", "Deactivated"):
            if message.signature != "osta{sv}" or message.path != PORTAL_PATH:
                return
            session, action = body[0], body[1]
            if not self._active or session != self._session or action not in self._wanted:
                return
            if member == "Activated":
                self._press(action)
            else:
                self._release(action)
        elif (
            interface == SESSION_INTERFACE
            and member == "Closed"
            and self._session is not None
            and message.path == self._session
        ):
            self._lost("session_closed")

    def _owner_changed(self, new_owner: object) -> None:
        if self._owner is None:
            return  # still starting: GetNameOwner will tell
        if new_owner != self._owner:
            self._lost("portal_gone")

    def _lost(self, reason: str) -> None:
        """The portal or our session is gone: release everything, then tell the caller."""
        was_active = self._active
        self._active = False
        self._session = None
        self._owner = None
        self._release_all()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        log.warning("hotkeys_portal_lost", reason=reason)
        if was_active and self._on_lost is not None:
            self._safely(self._on_lost)

    def _press(self, action: str) -> None:
        if action in self._pressed:
            return  # key repeat, or a second Activated without a Deactivated
        self._pressed.add(action)
        self._safely(self._on_pressed, action)

    def _release(self, action: str) -> None:
        if action not in self._pressed:
            return
        self._pressed.discard(action)
        self._safely(self._on_released, action)

    def _release_all(self) -> None:
        for action in [a for a in ACTIONS if a in self._pressed]:
            self._release(action)

    @staticmethod
    def _safely(callback: Callable[..., None], *args: str) -> None:
        try:
            callback(*args)
        except Exception as exc:
            log.warning("hotkeys_callback_failed", error=type(exc).__name__)


def _parse_shortcuts(value: object, wanted: frozenset[str]) -> dict[str, str]:
    """Read an ``a(sa{sv})`` shortcut list: our action ids -> sanitized trigger description."""
    if isinstance(value, Variant):
        if value.signature != "a(sa{sv})":
            return {}
        value = value.value
    if not isinstance(value, list):
        return {}
    found: dict[str, str] = {}
    for entry in value[:_MAX_SHORTCUTS]:
        if not (isinstance(entry, list | tuple) and len(entry) == 2):
            continue
        action, options = entry
        if not isinstance(action, str) or action not in wanted or not isinstance(options, dict):
            continue
        trigger = options.get("trigger_description")
        found[action] = sanitize_friendly(trigger.value if isinstance(trigger, Variant) else "")
    return found


class KGlobalAccelHotkeys:
    """Seam for the fallback when the portal is absent or the user dismissed its dialog.

    Planned (RESEARCH §4): register a component named after ``APP_ID`` with KGlobalAccel
    (``doRegister`` / ``setShortcutKeys``) and listen to the component's
    ``globalShortcutPressed`` / ``globalShortcutReleased`` signals, with the same
    ``on_pressed`` / ``on_released`` contract as ``PortalHotkeys``. Not implemented: its
    registration writes to KGlobalAccel's configuration, so it must be recorded in the
    install manifest and removed by the uninstaller before it may exist.
    """

    def __init__(
        self,
        bus_factory: Callable[[], Awaitable[MessageBus]] | None = None,
        *,
        on_pressed: Callable[[str], None],
        on_released: Callable[[str], None],
    ) -> None:
        self._bus_factory = bus_factory
        self._on_pressed = on_pressed
        self._on_released = on_released

    async def start(self, bindings: Mapping[str, KeyCombo | None]) -> BindResult:
        raise NotImplementedError("the KGlobalAccel hotkey fallback is not implemented yet")

    async def stop(self) -> None:
        """Nothing was started, so there is nothing to stop."""
