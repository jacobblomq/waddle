"""KWinLink against a fake bus (no session bus needed), plus one test on a private real bus."""

# No ``from __future__ import annotations``: dbus-fast evaluates method annotations eagerly.

import asyncio
import json
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
from dbus_fast import BusType, Message, MessageType, RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusBool, DBusInt32, DBusStr
from dbus_fast.service import ServiceInterface, dbus_method

from waddle_core.identity import APP_ID, DBUS_KWIN_INTERFACE, DBUS_PATH, PENGUIN_WINDOW_TITLE
from waddle_desktop.kwin import link as link_module
from waddle_desktop.kwin.link import KWinLink
from waddle_desktop.kwin.types import OutputInfo, OwnWindow

ALLOWED_KWIN_MEMBERS = {"loadScript", "isScriptLoaded", "unloadScript", "run"}
OWN_WINDOW = {
    "title": PENGUIN_WINDOW_TITLE,
    "uuid": "abc",
    "x": 10,
    "y": 20,
    "width": 180,
    "height": 220,
    "output": "DP-1",
}
OUTPUTS = {
    "outputs": [{"name": "DP-1", "x": 0, "y": 0, "width": 2560, "height": 1440}],
    "active": "DP-1",
}


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, 10))


def move_done_payload(source: str, ok: bool = True) -> str:
    """What the generated move script would send back, read from its baked-in values."""
    title = json.loads(re.search(r"var TITLE = (.*);", source).group(1))  # type: ignore[union-attr]
    x = int(re.search(r"var X = (-?\d+);", source).group(1))  # type: ignore[union-attr]
    y = int(re.search(r"var Y = (-?\d+);", source).group(1))  # type: ignore[union-attr]
    return json.dumps({"title": title, "ok": ok, "x": x, "y": y})


class FakeBus:
    """Records what KWinLink does and plays the bus daemon plus (optionally) KWin."""

    def __init__(
        self,
        *,
        kwin: bool = True,
        name_reply: RequestNameReply = RequestNameReply.PRIMARY_OWNER,
        stale_core: bool = False,
        move_reply: Callable[[str], str | None] = move_done_payload,
    ) -> None:
        self.kwin = kwin
        self.name_reply = name_reply
        self.loaded: dict[str, int] = {"waddle-core": 99} if stale_core else {}
        self.move_reply = move_reply
        self.events: list[tuple[Any, ...]] = []
        self.exported: dict[str, Any] = {}
        self.handlers: list[Callable[[Message], Any]] = []
        self.scripts: dict[int, tuple[str, str]] = {}  # id -> (file, plugin)
        self.seen_files: list[tuple[Path, int, int, str]] = []  # path, file mode, dir mode, text
        self.connected = True
        self._next_id = 0

    # -- the MessageBus surface KWinLink uses --

    def export(self, path: str, interface: Any) -> None:
        self.events.append(("export", path, interface.name))
        self.exported[path] = interface

    def unexport(self, path: str, interface: Any = None) -> None:
        self.events.append(("unexport", path))
        self.exported.pop(path, None)

    async def request_name(self, name: str, flags: Any = None) -> RequestNameReply:
        self.events.append(("request_name", name))
        return self.name_reply

    async def release_name(self, name: str) -> None:
        self.events.append(("release_name", name))

    def add_message_handler(self, handler: Callable[[Message], Any]) -> None:
        self.handlers.append(handler)

    def remove_message_handler(self, handler: Callable[[Message], Any]) -> None:
        self.handlers.remove(handler)

    def disconnect(self) -> None:
        self.events.append(("disconnect",))
        self.connected = False

    async def call(self, msg: Message) -> Message:
        msg.serial = len(self.events) + 1
        self.events.append(("call", msg.destination, msg.path, msg.interface, msg.member, msg.body))
        if msg.destination == "org.freedesktop.DBus":
            return Message.new_method_return(msg)
        assert msg.destination == "org.kde.KWin"
        if not self.kwin:
            return Message.new_error(msg, "org.freedesktop.DBus.Error.ServiceUnknown", "no KWin")
        if msg.member == "isScriptLoaded":
            return Message.new_method_return(msg, "b", [msg.body[0] in self.loaded])
        if msg.member == "unloadScript":
            return Message.new_method_return(
                msg, "b", [self.loaded.pop(msg.body[0], None) is not None]
            )
        if msg.member == "loadScript":
            file, plugin = msg.body
            self._next_id += 1
            self.loaded[plugin] = self._next_id
            self.scripts[self._next_id] = (file, plugin)
            return Message.new_method_return(msg, "i", [self._next_id])
        assert msg.member == "run" and msg.interface == "org.kde.kwin.Script"
        file, plugin = self.scripts[int(msg.path.removeprefix("/Scripting/Script"))]
        if plugin != "waddle-core":
            path = Path(file)
            text = path.read_text(encoding="utf-8")
            self.seen_files.append(
                (
                    path,
                    stat.S_IMODE(path.stat().st_mode),
                    stat.S_IMODE(path.parent.stat().st_mode),
                    text,
                )
            )
            reply = self.move_reply(text)
            if reply is not None:
                self.exported[DBUS_PATH].MoveDone(reply)
        return Message.new_method_return(msg)

    # -- helpers for assertions --

    def kwin_calls(self) -> list[tuple[str, str, list[Any]]]:
        return [
            (e[2], e[4], e[5]) for e in self.events if e[0] == "call" and e[1] == "org.kde.KWin"
        ]

    def kwin_owner_changed(self, new_owner: str) -> None:
        signal = Message(
            message_type=MessageType.SIGNAL,
            sender="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="NameOwnerChanged",
            signature="sss",
            body=["org.kde.KWin", "", new_owner],
        )
        for handler in list(self.handlers):
            handler(signal)


class Harness:
    def __init__(self, tmp_path: Path, bus: FakeBus | None = None) -> None:
        self.bus = bus or FakeBus()
        self.runtime_dir = tmp_path / "run" / "waddle"
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.core_script = tmp_path / "waddle-core.js"
        self.core_script.write_text("// core\n", encoding="utf-8")
        self.windows: list[OwnWindow] = []
        self.outputs: list[tuple[list[OutputInfo], str | None]] = []
        self.statuses: list[tuple[bool, str]] = []
        self.link = KWinLink(
            self.runtime_dir,
            self.core_script,
            on_own_window=self.windows.append,
            on_outputs=lambda outputs, active: self.outputs.append((outputs, active)),
            on_status=lambda ok, reason: self.statuses.append((ok, reason)),
            bus_factory=self._factory,
        )

    async def _factory(self) -> MessageBus:
        return self.bus  # type: ignore[return-value]

    @property
    def receiver(self) -> Any:
        return self.bus.exported[DBUS_PATH]


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_start_exports_claims_name_then_loads_and_runs_core(harness: Harness) -> None:
    assert run(harness.link.start()) is True

    bus = harness.bus
    assert bus.events[0] == ("export", DBUS_PATH, DBUS_KWIN_INTERFACE)
    assert bus.events[1] == ("request_name", APP_ID)
    assert bus.kwin_calls() == [
        ("/Scripting", "isScriptLoaded", ["waddle-core"]),
        ("/Scripting", "loadScript", [str(harness.core_script), "waddle-core"]),
        ("/Scripting/Script1", "run", []),
    ]
    assert harness.statuses == [(True, "KWin script loaded")]
    # The watch for a KWin restart is registered with the bus daemon.
    match = next(e[5][0] for e in bus.events if e[0] == "call" and e[4] == "AddMatch")
    assert "NameOwnerChanged" in match and "arg0='org.kde.KWin'" in match
    assert len(bus.handlers) == 1


def test_start_unloads_a_stale_core_script_first(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeBus(stale_core=True))
    assert run(harness.link.start()) is True
    assert [c[1] for c in harness.bus.kwin_calls()] == [
        "isScriptLoaded",
        "unloadScript",
        "loadScript",
        "run",
    ]


def test_exported_interface_has_the_four_one_string_methods(harness: Harness) -> None:
    run(harness.link.start())
    interface = harness.receiver.introspect()
    assert interface.name == DBUS_KWIN_INTERFACE
    assert {m.name: (m.in_signature, m.out_signature) for m in interface.methods} == {
        "Hello": ("s", ""),
        "OwnWindow": ("s", ""),
        "Outputs": ("s", ""),
        "MoveDone": ("s", ""),
    }


def test_payloads_reach_callbacks_parsed(harness: Harness) -> None:
    run(harness.link.start())
    harness.receiver.OwnWindow(json.dumps(OWN_WINDOW))
    harness.receiver.Outputs(json.dumps(OUTPUTS))
    harness.receiver.Hello('{"version": 1, "apiOk": true}')

    assert harness.windows == [OwnWindow(PENGUIN_WINDOW_TITLE, "abc", 10, 20, 180, 220, "DP-1")]
    assert harness.outputs == [([OutputInfo("DP-1", 0, 0, 2560, 1440)], "DP-1")]
    assert harness.statuses[-1] == (True, "KWin script running")


def test_hello_with_broken_api_reports_not_ok(harness: Harness) -> None:
    run(harness.link.start())
    harness.receiver.Hello('{"version": 1, "apiOk": false}')
    assert harness.statuses[-1][0] is False


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "{",
        "[]",
        '{"title": "Waddle"}',
        json.dumps({**OWN_WINDOW, "x": "10"}),
        json.dumps({**OWN_WINDOW, "width": 0}),
        json.dumps({**OWN_WINDOW, "title": "t" * 5000}),
        json.dumps(OWN_WINDOW).replace("10", "NaN"),
        json.dumps({"outputs": [{"name": "DP-1"}], "active": None}),
        json.dumps({"outputs": "DP-1"}),
    ],
)
def test_malformed_payloads_are_swallowed(
    harness: Harness, payload: str, caplog: pytest.LogCaptureFixture
) -> None:
    run(harness.link.start())
    statuses = list(harness.statuses)
    with caplog.at_level("DEBUG", logger="waddle"):
        for method in ("OwnWindow", "Outputs", "Hello", "MoveDone"):
            getattr(harness.receiver, method)(payload)  # must not raise
    assert harness.windows == [] and harness.outputs == []
    assert harness.statuses == statuses
    assert caplog.text.count("kwin_payload_rejected") == 4
    # Only the kind of failure is logged, never the payload.
    assert "ttttt" not in caplog.text and "DP-1" not in caplog.text


def test_a_raising_callback_does_not_escape_to_the_caller(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def boom(_window: OwnWindow) -> None:
        raise RuntimeError("ui went away")

    harness.link._on_own_window = boom
    run(harness.link.start())
    harness.receiver.OwnWindow(json.dumps(OWN_WINDOW))


def test_a_raising_status_callback_does_not_escape_start(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def boom(_ok: bool, _reason: str) -> None:
        raise RuntimeError("ui went away")

    harness.link._on_status = boom
    assert run(harness.link.start()) is True
    harness.receiver.Hello('{"version": 1, "apiOk": true}')
    harness.bus.kwin_owner_changed("")


def test_move_own_window_runs_a_private_one_shot_script(harness: Harness) -> None:
    async def scenario() -> bool:
        await harness.link.start()
        return await harness.link.move_own_window(PENGUIN_WINDOW_TITLE, -40, 900)

    assert run(scenario()) is True

    bus = harness.bus
    [(path, file_mode, dir_mode, text)] = bus.seen_files
    assert path.parent == harness.runtime_dir
    assert (file_mode, dir_mode) == (0o600, 0o700)
    assert "var X = -40;" in text and "var Y = 900;" in text
    plugin = path.stem
    assert plugin.startswith("waddle-move-") and plugin != "waddle-move-"
    assert bus.kwin_calls()[-3:] == [
        ("/Scripting", "loadScript", [str(path), plugin]),
        ("/Scripting/Script2", "run", []),
        ("/Scripting", "unloadScript", [plugin]),
    ]
    assert not path.exists() and list(harness.runtime_dir.iterdir()) == []
    assert plugin not in bus.loaded


def test_each_move_uses_a_unique_script(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.link.start()
        assert await harness.link.move_own_window(PENGUIN_WINDOW_TITLE, 1, 2)
        assert await harness.link.move_own_window(PENGUIN_WINDOW_TITLE, 1, 2)

    run(scenario())
    first, second = (seen[0] for seen in harness.bus.seen_files)
    assert first != second


def test_move_reports_false_when_the_script_found_no_window(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeBus(move_reply=lambda text: move_done_payload(text, ok=False)))

    async def scenario() -> bool:
        await harness.link.start()
        return await harness.link.move_own_window(PENGUIN_WINDOW_TITLE, 5, 5)

    assert run(scenario()) is False
    assert list(harness.runtime_dir.iterdir()) == []


@pytest.mark.parametrize(
    "reply",
    [
        None,  # the script never answers
        json.dumps({"title": PENGUIN_WINDOW_TITLE, "ok": True, "x": 6, "y": 5}),  # another move
        "garbage",
    ],
)
def test_move_times_out_and_still_cleans_up(tmp_path: Path, reply: str | None) -> None:
    harness = Harness(tmp_path, FakeBus(move_reply=lambda _text: reply))

    async def scenario() -> bool:
        await harness.link.start()
        return await harness.link.move_own_window(PENGUIN_WINDOW_TITLE, 5, 5, timeout=0.05)

    assert run(scenario()) is False
    [(path, _, _, _)] = harness.bus.seen_files
    assert not path.exists() and list(harness.runtime_dir.iterdir()) == []
    assert harness.bus.kwin_calls()[-1] == ("/Scripting", "unloadScript", [path.stem])


def test_move_cleans_up_when_kwin_is_gone(harness: Harness) -> None:
    async def scenario() -> bool:
        await harness.link.start()
        harness.bus.kwin = False
        return await harness.link.move_own_window(PENGUIN_WINDOW_TITLE, 5, 5)

    assert run(scenario()) is False
    assert list(harness.runtime_dir.iterdir()) == []


def test_move_rejects_foreign_titles_without_touching_kwin(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.link.start()
        before = len(harness.bus.events)
        with pytest.raises(ValueError):
            await harness.link.move_own_window("Mozilla Firefox", 0, 0)
        assert len(harness.bus.events) == before

    run(scenario())
    assert not harness.runtime_dir.exists()


def test_move_before_start_returns_false(harness: Harness) -> None:
    assert run(harness.link.move_own_window(PENGUIN_WINDOW_TITLE, 0, 0)) is False


def test_start_without_kwin_degrades_and_recovers_when_kwin_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(link_module, "RELOAD_DELAYS", (0.0, 0.01))
    harness = Harness(tmp_path, FakeBus(kwin=False))

    async def scenario() -> None:
        assert await harness.link.start() is False
        assert harness.statuses == [(False, "KWin is not available")]
        assert harness.bus.connected  # still listening for KWin to show up

        harness.bus.kwin = True
        harness.bus.kwin_owner_changed(":1.42")
        async with asyncio.timeout(2):
            while harness.statuses[-1] != (True, "KWin script reloaded"):
                await asyncio.sleep(0.005)

    run(scenario())
    assert [c[1] for c in harness.bus.kwin_calls()][-2:] == ["loadScript", "run"]


def test_kwin_going_away_is_reported(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.link.start()
        harness.bus.kwin_owner_changed("")

    run(scenario())
    assert harness.statuses[-1] == (False, "KWin is not running")


def test_start_when_another_waddle_owns_the_name(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeBus(name_reply=RequestNameReply.EXISTS))
    assert run(harness.link.start()) is False
    assert harness.statuses == [(False, "another Waddle is running")]
    assert harness.bus.kwin_calls() == []  # the running instance's script is left alone
    assert not harness.bus.connected
    assert ("release_name", APP_ID) not in harness.bus.events


def test_start_without_a_session_bus(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    async def no_bus() -> MessageBus:
        raise OSError("no bus")

    harness.link._bus_factory = no_bus
    assert run(harness.link.start()) is False
    assert harness.statuses == [(False, "session bus unavailable")]
    run(harness.link.stop())  # harmless


def test_start_with_missing_core_script(harness: Harness) -> None:
    harness.core_script.unlink()
    assert run(harness.link.start()) is False
    assert harness.statuses == [(False, "KWin script file is missing")]
    assert harness.bus.kwin_calls() == []


def test_stop_unloads_releases_and_disconnects(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.link.start()
        await harness.link.stop()
        await harness.link.stop()  # idempotent

    run(scenario())
    bus = harness.bus
    assert bus.kwin_calls()[-1] == ("/Scripting", "unloadScript", ["waddle-core"])
    assert "waddle-core" not in bus.loaded
    tail = [e[0] for e in bus.events if e[0] != "call"][-3:]
    assert tail == ["unexport", "release_name", "disconnect"]
    assert bus.handlers == [] and not bus.connected


def test_only_the_four_sanctioned_kwin_methods_are_ever_called(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.link.start()
        await harness.link.move_own_window(PENGUIN_WINDOW_TITLE, 1, 1)
        harness.bus.kwin_owner_changed(":1.7")
        await asyncio.sleep(0.05)
        await harness.link.stop()

    run(scenario())
    calls = harness.bus.kwin_calls()
    assert {member for _, member, _ in calls} == ALLOWED_KWIN_MEMBERS
    for path, member, _ in calls:
        assert path.startswith("/Scripting")
        assert (member == "run") == (path != "/Scripting")


# --- one integration test on a private, real bus ------------------------------------------


class FakeScripting(ServiceInterface):
    """A tiny org.kde.KWin: loads "scripts" and answers the way the real ones would."""

    def __init__(self, bus: MessageBus) -> None:
        super().__init__("org.kde.kwin.Scripting")
        self.bus = bus
        self.loaded: dict[str, int] = {}
        self.log: list[str] = []
        self.tasks: list[asyncio.Task[Any]] = []

    @dbus_method()
    def loadScript(self, file: DBusStr, plugin: DBusStr) -> DBusInt32:
        self.log.append(f"load:{plugin}")
        script_id = len(self.log)
        self.loaded[plugin] = script_id
        self.bus.export(f"/Scripting/Script{script_id}", FakeScript(self, file, plugin))
        return script_id

    @dbus_method()
    def isScriptLoaded(self, plugin: DBusStr) -> DBusBool:
        return plugin in self.loaded

    @dbus_method()
    def unloadScript(self, plugin: DBusStr) -> DBusBool:
        self.log.append(f"unload:{plugin}")
        return self.loaded.pop(plugin, None) is not None

    def call_back(self, method: str, payload: str) -> None:
        message = Message(
            destination=APP_ID,
            path=DBUS_PATH,
            interface=DBUS_KWIN_INTERFACE,
            member=method,
            signature="s",
            body=[payload],
        )
        self.tasks.append(asyncio.get_running_loop().create_task(self.bus.call(message)))


class FakeScript(ServiceInterface):
    def __init__(self, scripting: FakeScripting, file: str, plugin: str) -> None:
        super().__init__("org.kde.kwin.Script")
        self.scripting, self.file, self.plugin = scripting, file, plugin

    @dbus_method()
    def run(self) -> None:
        if self.plugin == "waddle-core":
            self.scripting.call_back("Hello", '{"version": 1, "apiOk": true}')
            self.scripting.call_back("Outputs", json.dumps(OUTPUTS))
            self.scripting.call_back("OwnWindow", json.dumps(OWN_WINDOW))
            self.scripting.call_back("OwnWindow", "not json")
        else:
            source = Path(self.file).read_text(encoding="utf-8")
            self.scripting.call_back("MoveDone", move_done_payload(source))


@pytest.fixture
def private_bus(tmp_path: Path) -> Iterator[str]:
    daemon = shutil.which("dbus-daemon")
    if daemon is None:
        pytest.skip("dbus-daemon not available")
    proc = subprocess.Popen(
        [daemon, "--session", "--nofork", "--nopidfile", "--print-address=1",
         f"--address=unix:path={tmp_path / 'bus'}"],
        stdout=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    try:
        assert proc.stdout is not None
        address = proc.stdout.readline().strip()
        if not address:
            pytest.skip("dbus-daemon did not start")
        yield address
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_link_end_to_end_on_a_private_bus(
    tmp_path: Path, private_bus: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(link_module, "RELOAD_DELAYS", (0.0, 0.05))

    async def connect() -> MessageBus:
        return await MessageBus(bus_address=private_bus, bus_type=BusType.SESSION).connect()

    harness = Harness(tmp_path)
    harness.link._bus_factory = connect
    second = Harness(tmp_path / "second")
    second.link._bus_factory = connect

    async def until(condition: Callable[[], bool]) -> None:
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(0.01)

    async def scenario() -> None:
        kwin_bus = await connect()
        kwin = FakeScripting(kwin_bus)
        kwin_bus.export("/Scripting", kwin)
        await kwin_bus.request_name("org.kde.KWin")
        try:
            assert await harness.link.start() is True
            await until(lambda: len(harness.windows) == 1 and len(harness.outputs) == 1)
            assert harness.windows[0].uuid == "abc"
            assert (True, "KWin script running") in harness.statuses

            # A second instance is told so and leaves the first one's script alone.
            assert await second.link.start() is False
            assert second.statuses == [(False, "another Waddle is running")]
            assert kwin.log == ["load:waddle-core"]

            assert await harness.link.move_own_window(PENGUIN_WINDOW_TITLE, 12, 34) is True
            assert [entry.split("-")[0] for entry in kwin.log[1:]] == [
                "load:waddle",
                "unload:waddle",
            ]
            assert list(harness.runtime_dir.iterdir()) == []

            # KWin restarts: its runtime scripts are gone, the link loads the core again.
            await kwin_bus.release_name("org.kde.KWin")
            await until(lambda: harness.statuses[-1] == (False, "KWin is not running"))
            kwin.loaded.clear()
            await kwin_bus.request_name("org.kde.KWin")
            await until(lambda: len(harness.windows) == 2 and harness.statuses[-1][0])
            assert kwin.log[-1] == "load:waddle-core"

            await harness.link.stop()
            assert kwin.log[-1] == "unload:waddle-core"
        finally:
            await harness.link.stop()
            await asyncio.gather(*kwin.tasks, return_exceptions=True)
            kwin_bus.disconnect()

    run(scenario())
