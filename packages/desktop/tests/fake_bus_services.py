"""Fake session services for tests, served on a PRIVATE bus started with dbus-daemon.

Nothing here talks to the real session or system bus. Each fake owns one connection and one
well-known name, answers at the message level (so it sees the caller's unique name, which
the portal's request paths depend on), and records every method call it receives.

Fakes: the desktop portal (host registry + GlobalShortcuts), KGlobalAccel, the screen saver,
logind and the notification server. ``Imposter`` is a connection that owns no name and sends
forged signals.
"""

import asyncio
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dbus_fast import BusType, DBusError, Message, MessageFlag, MessageType, Variant
from dbus_fast.aio import MessageBus

UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"
PROPERTIES = "org.freedesktop.DBus.Properties"

Reply = tuple[str, list[Any]] | None


@contextmanager
def private_bus(directory: Path) -> Iterator[str | None]:
    """Start a throwaway dbus-daemon; yields its address, or ``None`` if it cannot run."""
    daemon = shutil.which("dbus-daemon")
    if daemon is None:
        yield None
        return
    proc = subprocess.Popen(
        [daemon, "--session", "--nofork", "--nopidfile", "--print-address=1",
         f"--address=unix:path={directory / 'bus'}"],
        stdout=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    try:
        assert proc.stdout is not None
        address = proc.stdout.readline().strip()
        yield address or None
    finally:
        proc.terminate()
        proc.wait(timeout=5)


class InProcessHub:
    """A bus daemon in a dozen lines, for running the same scenarios with no dbus-daemon.

    It routes method calls to ``FakeService`` objects and their signals to every client, in
    the order they are sent (a signal sent while a call is being handled reaches the client
    before that call's reply, exactly like the real race). It does not marshal anything and
    does not filter by match rule; the private-bus runs cover those.
    """

    def __init__(self) -> None:
        self.names: dict[str, tuple[str, FakeService]] = {}
        self.clients: list[InProcessBus] = []
        self.match_rules: list[str] = []
        self.unrouted: list[str] = []  # destinations that were called while nobody owned them
        self._next = 1

    def unique(self) -> str:
        self._next += 1
        return f":1.{self._next}"

    def connector(self) -> Callable[[], Awaitable[Any]]:
        async def connect() -> InProcessBus:
            client = InProcessBus(self, self.unique())
            self.clients.append(client)
            return client

        return connect

    def deliver(self, msg: Message) -> None:
        for client in list(self.clients):
            if client.connected and msg.destination in (None, client.unique_name):
                client.receive(msg)

    def owner_changed(self, name: str, old: str, new: str) -> None:
        self.deliver(
            Message(
                message_type=MessageType.SIGNAL,
                sender="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="NameOwnerChanged",
                signature="sss",
                body=[name, old, new],
            )
        )


class _ServiceEnd:
    """What a ``FakeService`` holds instead of a MessageBus when it runs on a hub."""

    def __init__(self, hub: InProcessHub, service: "FakeService") -> None:
        self.hub, self.service, self.unique_name = hub, service, hub.unique()
        self.connected = True

    def add_message_handler(self, handler: Any) -> None:
        pass

    async def request_name(self, name: str) -> None:
        self.hub.names[name] = (self.unique_name, self.service)
        self.hub.owner_changed(name, "", self.unique_name)

    def send(self, msg: Message) -> None:
        msg.sender = self.unique_name
        self.hub.deliver(msg)

    def disconnect(self) -> None:
        self.connected = False
        for name, (unique, _service) in list(self.hub.names.items()):
            if unique == self.unique_name:
                del self.hub.names[name]
                self.hub.owner_changed(name, unique, "")

    async def wait_for_disconnect(self) -> None:
        return None


class InProcessBus:
    """The client side: the part of ``MessageBus`` the modules under test use."""

    def __init__(self, hub: InProcessHub, unique_name: str) -> None:
        self.hub, self.unique_name = hub, unique_name
        self.connected = True
        self._handlers: list[Callable[[Message], Any]] = []
        self._gone: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._serial = 0
        self._late: dict[int, asyncio.Future[Message]] = {}  # calls answered after the fact

    def add_message_handler(self, handler: Callable[[Message], Any]) -> None:
        self._handlers.append(handler)

    def remove_message_handler(self, handler: Callable[[Message], Any]) -> None:
        self._handlers.remove(handler)

    def receive(self, msg: Message) -> None:
        if msg.message_type is MessageType.METHOD_RETURN:
            late = self._late.pop(msg.reply_serial, None)
            if late is not None and not late.done():
                late.set_result(msg)
            return
        for handler in list(self._handlers):
            handler(msg)

    def disconnect(self) -> None:
        self.connected = False
        if not self._gone.done():
            self._gone.set_result(None)

    async def wait_for_disconnect(self) -> None:
        await asyncio.shield(self._gone)

    async def call(self, msg: Message) -> Message:
        await asyncio.sleep(0)
        if not self.connected:
            raise ConnectionError("not connected")
        msg.sender = self.unique_name
        self._serial += 1
        msg.serial = self._serial
        if msg.destination == "org.freedesktop.DBus":
            if msg.member == "GetNameOwner":
                if msg.body[0] not in self.hub.names:
                    return Message.new_error(
                        msg, "org.freedesktop.DBus.Error.NameHasNoOwner", "no owner"
                    )
                return Message.new_method_return(msg, "s", [self.hub.names[msg.body[0]][0]])
            if msg.member in ("AddMatch", "RemoveMatch"):
                self.hub.match_rules.append(msg.body[0])
                return Message.new_method_return(msg, "", [])
            return Message.new_error(msg, UNKNOWN_METHOD, "no such method")
        if msg.destination not in self.hub.names:
            self.hub.unrouted.append(msg.destination or "")
            return Message.new_error(
                msg, "org.freedesktop.DBus.Error.ServiceUnknown", "no such service"
            )
        unique, service = self.hub.names[msg.destination]
        reply = service._on_message(msg)
        if reply is True:  # unanswered for now; the service may still reply through send()
            self._late[msg.serial] = asyncio.get_running_loop().create_future()
            reply = await self._late[msg.serial]
        if not isinstance(reply, Message):
            return Message.new_error(msg, UNKNOWN_METHOD, "no such method")
        reply.sender = unique
        await asyncio.sleep(0)
        return reply


def connector(where: "str | InProcessHub") -> Callable[[], Awaitable[Any]]:
    if isinstance(where, InProcessHub):
        return where.connector()

    async def connect() -> MessageBus:
        return await MessageBus(bus_address=where, bus_type=BusType.SESSION).connect()

    return connect


@contextmanager
def transport(kind: str, directory: Path) -> Iterator["str | InProcessHub | None"]:
    """``"inprocess"`` -> a hub; ``"daemon"`` -> a private bus address or ``None``."""
    if kind == "inprocess":
        yield InProcessHub()
        return
    with private_bus(directory) as address:
        yield address


async def until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def settle(seconds: float = 0.1) -> None:
    """Give signals that should NOT have an effect the time to (wrongly) have one."""
    await asyncio.sleep(seconds)


class FakeService:
    """Base: owns ``NAME`` and dispatches calls to ``handle``; unknown calls get an error."""

    NAME = ""

    def __init__(self) -> None:
        self.bus: Any = None
        self.calls: list[tuple[str, str, str]] = []  # (path, interface, member)
        # Members called WITHOUT the no-auto-start flag: such a call would have started the
        # service had it not been running.
        self.may_autostart: list[str] = []

    @property
    def members_called(self) -> set[str]:
        return {member for _path, _interface, member in self.calls}

    async def start(self, where: "str | InProcessHub") -> "FakeService":
        if isinstance(where, InProcessHub):
            self.bus = _ServiceEnd(where, self)
        else:
            self.bus = await connector(where)()
        self.bus.add_message_handler(self._on_message)
        if self.NAME:
            await self.bus.request_name(self.NAME)
        return self

    async def stop(self) -> None:
        """Leave the bus, as a crashing or exiting service would."""
        if self.bus is not None:
            self.bus.disconnect()
            await self.bus.wait_for_disconnect()
            self.bus = None

    def _on_message(self, msg: Message) -> Message | bool:
        if msg.message_type is not MessageType.METHOD_CALL or msg.destination is None:
            return False
        if msg.interface in ("org.freedesktop.DBus.Peer", "org.freedesktop.DBus.Introspectable"):
            return False
        self.calls.append((msg.path or "", msg.interface or "", msg.member or ""))
        if not MessageFlag.NO_AUTOSTART & MessageFlag(int(msg.flags)):
            self.may_autostart.append(msg.member or "")
        try:
            reply = self.handle(msg)
        except DBusError as exc:
            return Message.new_error(msg, exc.type, exc.text)
        if reply is None:
            return True  # handled, deliberately unanswered
        signature, body = reply
        return Message.new_method_return(msg, signature, body)

    def handle(self, msg: Message) -> Reply:
        raise DBusError(UNKNOWN_METHOD, "no such method")

    def signal(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str,
        body: list[Any],
        destination: str | None = None,
    ) -> None:
        assert self.bus is not None
        self.bus.send(
            Message(
                message_type=MessageType.SIGNAL,
                destination=destination,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body,
            )
        )


class Imposter(FakeService):
    """A peer with no well-known name, for forged signals."""


# ---------------------------------------------------------------------------------------------
# Desktop portal
# ---------------------------------------------------------------------------------------------

PORTAL_PATH = "/org/freedesktop/portal/desktop"
SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
REGISTRY = "org.freedesktop.host.portal.Registry"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"


class FakePortal(FakeService):
    NAME = "org.freedesktop.portal.Desktop"

    def __init__(self) -> None:
        super().__init__()
        self.registry = "ok"  # "ok" | "already" | "missing"
        self.has_shortcuts = True
        self.respond_before_return = True  # the race: Response overtakes the method reply
        self.legacy_request_paths = False  # pick our own request path, as old portals did
        self.create_response = 0
        self.bind_response = 0  # 1 = the user cancelled
        self.bind_silent = False  # never send a Response to BindShortcuts
        self.refuse_ids: set[str] = set()  # ids the "user" unticked in the dialog
        self.trigger_text = "Meta+Something"
        self.foreign_session_handle = False
        self.registered: list[str] = []
        self.order: list[str] = []  # member names in arrival order
        self.sessions: dict[str, dict[str, Any]] = {}  # session path -> {owner, shortcuts}
        self.bind_requests: list[list[Any]] = []
        self.closed_sessions: list[str] = []
        self.closed_requests: list[str] = []
        self.tokens: list[str] = []
        self._counter = 0

    def _sender_part(self, msg: Message) -> str:
        assert msg.sender is not None
        return msg.sender.removeprefix(":").replace(".", "_")

    def _respond(
        self, msg: Message, options: dict[str, Variant], code: int, results: dict[str, Variant]
    ) -> Reply:
        token = options["handle_token"].value
        self.tokens.append(token)
        path = f"{PORTAL_PATH}/request/{self._sender_part(msg)}/{token}"
        if self.legacy_request_paths:
            self._counter += 1
            path = f"{PORTAL_PATH}/request/legacy/r{self._counter}"

        def emit() -> None:
            if self.bus is not None:
                self.signal(path, REQUEST, "Response", "ua{sv}", [code, results], msg.sender)

        if self.respond_before_return and not self.legacy_request_paths:
            emit()
        else:
            asyncio.get_running_loop().call_later(0.05, emit)
        return "o", [path]

    def handle(self, msg: Message) -> Reply:
        self.order.append(msg.member or "")
        if msg.interface == REGISTRY and msg.member == "Register":
            if self.registry == "missing":
                raise DBusError("org.freedesktop.DBus.Error.UnknownInterface", "no registry")
            if self.registry == "already":
                raise DBusError(
                    "org.freedesktop.portal.Error.Failed", "Connection already associated"
                )
            assert msg.signature == "sa{sv}"
            self.registered.append(msg.body[0])
            return "", []
        if msg.interface == SESSION and msg.member == "Close":
            self.closed_sessions.append(msg.path or "")
            self.sessions.pop(msg.path or "", None)
            return "", []
        if msg.interface == REQUEST and msg.member == "Close":
            self.closed_requests.append(msg.path or "")
            return "", []
        if msg.interface != SHORTCUTS or not self.has_shortcuts or msg.path != PORTAL_PATH:
            raise DBusError(UNKNOWN_METHOD, "no such method")
        if msg.member == "CreateSession":
            assert msg.signature == "a{sv}"
            options = msg.body[0]
            session_token = options["session_handle_token"].value
            session = f"{PORTAL_PATH}/session/{self._sender_part(msg)}/{session_token}"
            if self.foreign_session_handle:
                session = f"{PORTAL_PATH}/session/9_99/someone_else"
            self.sessions[session] = {"owner": msg.sender, "shortcuts": []}
            return self._respond(
                msg, options, self.create_response, {"session_handle": Variant("s", session)}
            )
        if msg.member == "BindShortcuts":
            assert msg.signature == "oa(sa{sv})sa{sv}"
            session, shortcuts, parent, options = msg.body
            assert parent == ""
            self.bind_requests.append(shortcuts)
            if self.bind_silent:
                self.tokens.append(options["handle_token"].value)
                token = options["handle_token"].value
                return "o", [f"{PORTAL_PATH}/request/{self._sender_part(msg)}/{token}"]
            bound = [
                [ident, {**opts, "trigger_description": Variant("s", self.trigger_text)}]
                for ident, opts in shortcuts
                if ident not in self.refuse_ids
            ]
            if self.bind_response == 0:
                self.sessions[session]["shortcuts"] = bound
            results = {"shortcuts": Variant("a(sa{sv})", bound)} if self.bind_response == 0 else {}
            return self._respond(msg, options, self.bind_response, results)
        if msg.member == "ListShortcuts":
            assert msg.signature == "oa{sv}"
            session, options = msg.body
            bound = self.sessions[session]["shortcuts"]
            return self._respond(msg, options, 0, {"shortcuts": Variant("a(sa{sv})", bound)})
        raise DBusError(UNKNOWN_METHOD, "no such method")

    @property
    def session(self) -> str:
        return next(iter(self.sessions))

    def activated(self, ident: str, session: str | None = None, member: str = "Activated") -> None:
        self.signal(
            PORTAL_PATH, SHORTCUTS, member, "osta{sv}", [session or self.session, ident, 1, {}]
        )

    def deactivated(self, ident: str, session: str | None = None) -> None:
        self.activated(ident, session, "Deactivated")

    def close_session(self) -> None:
        self.signal(self.session, SESSION, "Closed", "a{sv}", [{}])


# ---------------------------------------------------------------------------------------------
# KGlobalAccel
# ---------------------------------------------------------------------------------------------


class FakeKGlobalAccel(FakeService):
    NAME = "org.kde.kglobalaccel"

    def __init__(self) -> None:
        super().__init__()
        self.by_key = "ok"  # "ok" | "missing" | "other-signature"
        self.available = "ok"  # "ok" | "missing"
        # Qt key int -> [(action unique, action friendly, component unique, component friendly)]
        self.held: dict[int, list[tuple[str, str, str, str]]] = {}
        self.queries: list[tuple[str, int]] = []

    def handle(self, msg: Message) -> Reply:
        if msg.path != "/kglobalaccel" or msg.interface != "org.kde.KGlobalAccel":
            raise DBusError(UNKNOWN_METHOD, "no such method")
        if msg.member == "globalShortcutsByKey" and self.by_key != "missing":
            assert msg.signature == "(ai)i"
            (keys,), match_type = msg.body
            assert len(keys) == 4 and keys[1:] == [0, 0, 0] and match_type == 0
            self.queries.append(("by-key", keys[0]))
            if self.by_key == "other-signature":
                return "as", [["surprise"]]
            infos = [
                [a_unique, a_friendly, c_unique, c_friendly, "default", "Default", [keys[0]], []]
                for a_unique, a_friendly, c_unique, c_friendly in self.held.get(keys[0], [])
            ]
            return "a(ssssssaiai)", [infos]
        if msg.member == "globalShortcutAvailable" and self.available != "missing":
            assert msg.signature == "(ai)s"
            (keys,), component = msg.body
            self.queries.append(("available", keys[0]))
            others = [h for h in self.held.get(keys[0], []) if h[2] != component]
            return "b", [not others]
        raise DBusError(UNKNOWN_METHOD, "no such method")


# ---------------------------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------------------------


class FakeScreenSaver(FakeService):
    NAME = "org.freedesktop.ScreenSaver"

    def __init__(self, active: bool = False, paths: tuple[str, ...] = ("/ScreenSaver",)) -> None:
        super().__init__()
        self.active = active
        self.paths = paths

    def handle(self, msg: Message) -> Reply:
        if (
            msg.path in self.paths
            and msg.interface == "org.freedesktop.ScreenSaver"
            and msg.member == "GetActive"
        ):
            return "b", [self.active]
        raise DBusError(UNKNOWN_METHOD, "no such method")

    def set_active(self, active: bool) -> None:
        self.active = active
        self.signal(self.paths[0], "org.freedesktop.ScreenSaver", "ActiveChanged", "b", [active])


class FakeNotifications(FakeService):
    NAME = "org.freedesktop.Notifications"
    PATH = "/org/freedesktop/Notifications"

    def __init__(self, inhibited: bool | None = False) -> None:
        super().__init__()
        self.inhibited = inhibited  # None: a server without the KDE extension

    def handle(self, msg: Message) -> Reply:
        if msg.path == self.PATH and msg.interface == PROPERTIES and msg.member == "Get":
            interface, name = msg.body
            if interface == self.NAME and name == "Inhibited" and self.inhibited is not None:
                return "v", [Variant("b", self.inhibited)]
            raise DBusError("org.freedesktop.DBus.Error.UnknownProperty", "no such property")
        raise DBusError(UNKNOWN_METHOD, "no such method")

    def set_inhibited(self, inhibited: bool, *, invalidate_only: bool = False) -> None:
        self.inhibited = inhibited
        changed = {} if invalidate_only else {"Inhibited": Variant("b", inhibited)}
        invalidated = ["Inhibited"] if invalidate_only else []
        self.signal(
            self.PATH,
            PROPERTIES,
            "PropertiesChanged",
            "sa{sv}as",
            [self.NAME, changed, invalidated],
        )


class FakeLogind(FakeService):
    NAME = "org.freedesktop.login1"
    MANAGER = "org.freedesktop.login1.Manager"
    SESSION = "org.freedesktop.login1.Session"
    SESSION_PATH = "/org/freedesktop/login1/session/_37"
    OTHER_SESSION_PATH = "/org/freedesktop/login1/session/_99"

    def __init__(self, locked: bool = False, idle: bool = False) -> None:
        super().__init__()
        self.locked = locked
        self.idle = idle
        self.session_ids: dict[str, str] = {"7": self.SESSION_PATH}
        self.pids: dict[int, str] = {}
        self.display: tuple[str, str] | None = None
        self.hold_properties: asyncio.Event | None = None  # set: answer Get only when released
        self._held: list[Message] = []

    def _props(self, msg: Message) -> Reply:
        interface, name = msg.body
        if msg.path == "/org/freedesktop/login1/user/self":
            if interface == "org.freedesktop.login1.User" and name == "Display" and self.display:
                return "v", [Variant("(so)", list(self.display))]
            raise DBusError("org.freedesktop.DBus.Error.UnknownProperty", "none")
        if msg.path == self.SESSION_PATH and interface == self.SESSION:
            if name == "LockedHint":
                return "v", [Variant("b", self.locked)]
            if name == "IdleHint":
                return "v", [Variant("b", self.idle)]
        raise DBusError("org.freedesktop.DBus.Error.UnknownProperty", "none")

    def handle(self, msg: Message) -> Reply:
        if msg.interface == PROPERTIES and msg.member == "Get":
            if self.hold_properties is not None:
                self._held.append(msg)
                return None
            return self._props(msg)
        if msg.path == "/org/freedesktop/login1" and msg.interface == self.MANAGER:
            if msg.member == "GetSession" and msg.body[0] in self.session_ids:
                return "o", [self.session_ids[msg.body[0]]]
            if msg.member == "GetSessionByPID" and msg.body[0] in self.pids:
                return "o", [self.pids[msg.body[0]]]
            if msg.member in ("GetSession", "GetSessionByPID"):
                raise DBusError("org.freedesktop.login1.NoSuchSession", "no session")
        raise DBusError(UNKNOWN_METHOD, "no such method")

    def release_properties(self) -> None:
        """Answer the Get calls that were being held back, with the current values."""
        assert self.bus is not None
        self.hold_properties = None
        held, self._held = self._held, []
        for msg in held:
            signature, body = self._props(msg) or ("", [])
            self.bus.send(Message.new_method_return(msg, signature, body))

    def set_hint(self, name: str, value: bool, *, path: str | None = None) -> None:
        if path is None:
            if name == "LockedHint":
                self.locked = value
            else:
                self.idle = value
        self.signal(
            path or self.SESSION_PATH,
            PROPERTIES,
            "PropertiesChanged",
            "sa{sv}as",
            [self.SESSION, {name: Variant("b", value)}, []],
        )

    def emit(self, member: str, path: str | None = None) -> None:
        """``Lock`` or ``Unlock`` on the session object."""
        self.signal(path or self.SESSION_PATH, self.SESSION, member, "", [])
