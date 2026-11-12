"""KWinLink against a fake bus (no session bus needed), plus one test on a private real bus."""

# No ``from __future__ import annotations``: dbus-fast evaluates method annotations eagerly.

import asyncio
import json
import os
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
from waddle_core.window import Geometry, WindowInfo
from waddle_desktop.kwin import link as link_module
from waddle_desktop.kwin.link import KWinLink
from waddle_desktop.kwin.types import CursorPos, OutputInfo, OwnWindow, Popup, PopupSet, WindowAt

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


UUID_A = "11111111-2222-4333-8444-555555555555"
UUID_MENU = "11111111-2222-4333-8444-666666666666"
WINDOW = {
    "uuid": UUID_A,
    "caption": "notes.txt — Kate",
    "resourceClass": "org.kde.kate",
    "resourceName": "kate",
    "desktopFile": "org.kde.kate",
    "pid": 4242,
    "frame": {"x": 10, "y": 20, "width": 800, "height": 600},
    "client": {"x": 10, "y": 48, "width": 800, "height": 572},
    "output": "DP-1",
    "scale": 1.25,
    "fullscreen": False,
    "xwayland": None,
    "own": False,
}
WINDOW_INFO = WindowInfo(
    uuid=UUID_A,
    caption="notes.txt — Kate",
    resource_class="org.kde.kate",
    resource_name="kate",
    desktop_file="org.kde.kate",
    pid=4242,
    frame=Geometry(10, 20, 800, 600),
    client=Geometry(10, 48, 800, 572),
    output="DP-1",
    scale=1.25,
    fullscreen=False,
    xwayland=None,
    is_own=False,
)
POPUPS = {
    "target": UUID_A,
    "popups": [{"uuid": UUID_MENU, "x": 30, "y": 60, "width": 120, "height": 240}],
}
NEW_METHODS = ("Activated", "TargetChanged", "Popups", "Cursor", "DesktopChanged", "WindowAt")


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, 10))


def move_done_payload(source: str, ok: bool = True) -> str:
    """What the generated move script would send back, read from its baked-in values."""
    title = json.loads(re.search(r"var TITLE = (.*);", source).group(1))  # type: ignore[union-attr]
    x = int(re.search(r"var X = (-?\d+);", source).group(1))  # type: ignore[union-attr]
    y = int(re.search(r"var Y = (-?\d+);", source).group(1))  # type: ignore[union-attr]
    return json.dumps({"title": title, "ok": ok, "x": x, "y": y})


def window_at_payload(source: str, found: dict[str, Any] | None = WINDOW) -> str:
    """What the generated window-at script would send back, read from its baked-in values."""
    x = int(re.search(r"var X = (-?\d+);", source).group(1))  # type: ignore[union-attr]
    y = int(re.search(r"var Y = (-?\d+);", source).group(1))  # type: ignore[union-attr]
    return json.dumps({"x": x, "y": y, "window": found})


def cursor_payload(_source: str) -> str:
    return json.dumps({"x": 640.5, "y": -12})


class FakeBus:
    """Records what KWinLink does and plays the bus daemon plus (optionally) KWin."""

    def __init__(
        self,
        *,
        kwin: bool = True,
        name_reply: RequestNameReply = RequestNameReply.PRIMARY_OWNER,
        stale_core: bool = False,
        stale_cursor: bool = False,
        move_reply: Callable[[str], str | None] = move_done_payload,
        query_reply: Callable[[str], str | None] = cursor_payload,
        at_reply: Callable[[str], str | None] = window_at_payload,
    ) -> None:
        self.kwin = kwin
        self.name_reply = name_reply
        self.loaded: dict[str, int] = {"waddle-core": 99} if stale_core else {}
        if stale_cursor:
            self.loaded["waddle-cursor"] = 98
        self.fail_members: set[str] = set()  # KWin members that answer with an error
        self.replies = {"move": move_reply, "query": query_reply, "at": at_reply}
        self.events: list[tuple[Any, ...]] = []
        self.exported: dict[str, Any] = {}
        self.handlers: list[Callable[[Message], Any]] = []
        self.scripts: dict[int, tuple[str, str]] = {}  # id -> (file, plugin)
        self.seen_files: list[tuple[Path, int, int, str]] = []  # path, file mode, dir mode, text
        self.connected = True
        self.autostart_calls: list[str] = []  # members called without the no-auto-start flag
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
        if not int(msg.flags) & 2:  # DBUS_HEADER_FLAG_NO_AUTO_START
            self.autostart_calls.append(msg.member or "")
        self.events.append(("call", msg.destination, msg.path, msg.interface, msg.member, msg.body))
        if msg.destination == "org.freedesktop.DBus":
            return Message.new_method_return(msg)
        assert msg.destination == "org.kde.KWin"
        if not self.kwin:
            return Message.new_error(msg, "org.freedesktop.DBus.Error.ServiceUnknown", "no KWin")
        if msg.member in self.fail_members:
            return Message.new_error(msg, "org.freedesktop.DBus.Error.Failed", "no")
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
        if plugin not in ("waddle-core", "waddle-cursor"):
            kind = plugin.split("-")[1]
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
            reply = self.replies[kind](text)
            if reply is not None:
                method = {"move": "MoveDone", "query": "Cursor", "at": "WindowAt"}[kind]
                getattr(self.exported[DBUS_PATH], method)(reply)
        return Message.new_method_return(msg)

    # -- helpers for assertions --

    def kwin_calls(self) -> list[tuple[str, str, list[Any]]]:
        return [
            (e[2], e[4], e[5]) for e in self.events if e[0] == "call" and e[1] == "org.kde.KWin"
        ]

    def kwin_owner_changed(self, new_owner: str, sender: str = "org.freedesktop.DBus") -> None:
        signal = Message(
            message_type=MessageType.SIGNAL,
            sender=sender,
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
        self.cursor_script = tmp_path / "waddle-cursor.js"
        self.cursor_script.write_text("// cursor\n", encoding="utf-8")
        self.windows: list[OwnWindow] = []
        self.outputs: list[tuple[list[OutputInfo], str | None]] = []
        self.statuses: list[tuple[bool, str]] = []
        self.activated: list[WindowInfo] = []
        self.changed: list[WindowInfo] = []
        self.popups: list[PopupSet] = []
        self.cursor: list[CursorPos] = []
        self.desktop_changes = 0
        self.link = KWinLink(
            self.runtime_dir,
            self.core_script,
            on_own_window=self.windows.append,
            on_outputs=lambda outputs, active: self.outputs.append((outputs, active)),
            on_status=lambda ok, reason: self.statuses.append((ok, reason)),
            on_activated=self.activated.append,
            on_target_changed=self.changed.append,
            on_popups=self.popups.append,
            on_cursor=self.cursor.append,
            on_desktop_changed=self._desktop_changed,
            cursor_script=self.cursor_script,
            bus_factory=self._factory,
        )

    def _desktop_changed(self) -> None:
        self.desktop_changes += 1

    def new_callback_counts(self) -> list[int]:
        return [
            len(self.activated),
            len(self.changed),
            len(self.popups),
            len(self.cursor),
            self.desktop_changes,
        ]

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


def test_exported_interface_has_exactly_the_ten_one_string_methods(harness: Harness) -> None:
    run(harness.link.start())
    interface = harness.receiver.introspect()
    assert interface.name == DBUS_KWIN_INTERFACE
    names = ("Hello", "OwnWindow", "Outputs", "MoveDone", *NEW_METHODS)
    assert len(names) == 10
    assert {m.name: (m.in_signature, m.out_signature) for m in interface.methods} == dict.fromkeys(
        names, ("s", "")
    )
    assert interface.signals == [] and interface.properties == []


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


def test_a_forged_name_owner_change_is_ignored(harness: Harness) -> None:
    # Only the bus daemon can say that KWin left or came back; a peer's signal with the
    # same member must neither report KWin gone nor make the link reload its scripts.
    async def scenario() -> None:
        await harness.link.start()
        await harness.link.set_cursor_stream(True)
        before = (list(harness.statuses), len(harness.bus.kwin_calls()))
        harness.bus.kwin_owner_changed("", sender=":1.666")
        harness.bus.kwin_owner_changed(":1.667", sender=":1.666")
        await asyncio.sleep(0.05)
        assert (list(harness.statuses), len(harness.bus.kwin_calls())) == before
        # The cursor script still counts as loaded: switching it on again calls nothing.
        assert await harness.link.set_cursor_stream(True)
        assert len(harness.bus.kwin_calls()) == before[1]

    run(scenario())


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
        await harness.link.set_cursor_stream(True)
        await harness.link.query_cursor()
        await harness.link.window_at(1, 1)
        harness.bus.kwin_owner_changed(":1.7")
        await asyncio.sleep(0.05)
        await harness.link.set_cursor_stream(False)
        await harness.link.stop()

    run(scenario())
    calls = harness.bus.kwin_calls()
    assert {member for _, member, _ in calls} == ALLOWED_KWIN_MEMBERS
    # Waddle never starts a compositor (or anything else) by calling it.
    assert harness.bus.autostart_calls == []
    for path, member, _ in calls:
        assert path.startswith("/Scripting")
        assert (member == "run") == (path != "/Scripting")


# --- Phase 2: activation, target, popups, desktop -----------------------------------------


def test_new_payloads_reach_their_callbacks_parsed(harness: Harness) -> None:
    run(harness.link.start())
    receiver = harness.receiver
    receiver.Activated(json.dumps(WINDOW))
    receiver.Activated(json.dumps({**WINDOW, "own": True, "caption": "Waddle Settings"}))
    receiver.TargetChanged(json.dumps({**WINDOW, "fullscreen": True, "xwayland": True}))
    receiver.Popups(json.dumps(POPUPS))
    receiver.Popups(json.dumps({"target": UUID_A, "popups": []}))
    receiver.DesktopChanged("{}")

    assert harness.activated[0] == WINDOW_INFO
    assert [(w.is_own, w.caption) for w in harness.activated] == [
        (False, "notes.txt — Kate"),
        (True, "Waddle Settings"),
    ]
    [changed] = harness.changed
    assert (changed.uuid, changed.fullscreen, changed.xwayland) == (UUID_A, True, True)
    assert harness.popups == [
        PopupSet(UUID_A, (Popup(UUID_MENU, Geometry(30, 60, 120, 240)),)),
        PopupSet(UUID_A, ()),
    ]
    assert harness.desktop_changes == 1
    # Nothing leaked into the Phase 1 callbacks, and nothing was sent to KWin for it.
    assert harness.windows == [] and harness.outputs == [] and harness.cursor == []
    assert [c[1] for c in harness.bus.kwin_calls()] == ["isScriptLoaded", "loadScript", "run"]


def test_untrusted_caption_is_cleaned_on_the_way_in(harness: Harness) -> None:
    run(harness.link.start())
    harness.receiver.Activated(json.dumps({**WINDOW, "caption": "a\x00b\n" + "c" * 2000}))
    assert harness.activated[0].caption == "ab" + "c" * 510


def test_the_new_callbacks_are_optional(tmp_path: Path) -> None:
    bus = FakeBus()

    async def factory() -> MessageBus:
        return bus  # type: ignore[return-value]

    statuses: list[tuple[bool, str]] = []
    link = KWinLink(  # exactly how the Phase 1 app constructs it
        tmp_path / "run",
        Harness(tmp_path).core_script,
        on_own_window=lambda _window: None,
        on_outputs=lambda _outputs, _active: None,
        on_status=lambda ok, reason: statuses.append((ok, reason)),
        bus_factory=factory,
    )

    async def scenario() -> None:
        assert await link.start() is True
        receiver = bus.exported[DBUS_PATH]
        receiver.Activated(json.dumps(WINDOW))
        receiver.TargetChanged(json.dumps(WINDOW))
        receiver.Popups(json.dumps(POPUPS))
        receiver.Cursor('{"x": 1, "y": 2}')
        receiver.DesktopChanged("{}")
        receiver.WindowAt(json.dumps({"x": 1, "y": 2, "window": None}))
        # Without a cursor script the stream cannot be switched on, and says so.
        assert await link.set_cursor_stream(True) is False
        assert await link.set_cursor_stream(False) is True
        await link.stop()

    run(scenario())
    assert statuses == [(True, "KWin script loaded")]
    assert "waddle-cursor" not in json.dumps(bus.kwin_calls())


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "{",
        "[]",
        "null",
        json.dumps({**WINDOW, "uuid": "not-a-uuid"}),
        json.dumps({**WINDOW, "caption": 7}),
        json.dumps({**WINDOW, "pid": -1}),
        json.dumps({**WINDOW, "frame": {"x": 0, "y": 0, "width": 1e9, "height": 1}}),
        json.dumps({**WINDOW, "caption": "SECRET-TITLE" * 2000}),
        json.dumps({**POPUPS, "target": "SECRET-TITLE"}),
        json.dumps({"x": "SECRET-TITLE", "y": 1, "window": None}),
        json.dumps(WINDOW).replace("1.25", "NaN"),
    ],
)
def test_malformed_new_payloads_are_swallowed_and_not_logged(
    harness: Harness, payload: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        await harness.link.start()
        await harness.link.set_cursor_stream(True)
        with caplog.at_level("DEBUG", logger="waddle"):
            for method in NEW_METHODS:
                if method != "DesktopChanged":
                    getattr(harness.receiver, method)(payload)  # must not raise

    run(scenario())
    assert harness.new_callback_counts() == [0, 0, 0, 0, 0]
    assert caplog.text.count("kwin_payload_rejected") == 5
    assert "SECRET" not in caplog.text and "Kate" not in caplog.text


def test_desktop_changed_needs_a_json_object_and_reads_nothing_from_it(harness: Harness) -> None:
    run(harness.link.start())
    for payload in ("", "[]", "nonsense", '"{}"'):
        harness.receiver.DesktopChanged(payload)
    assert harness.desktop_changes == 0
    harness.receiver.DesktopChanged('{"anything": "ignored"}')
    assert harness.desktop_changes == 1


@pytest.mark.parametrize(
    ("attribute", "method", "payload"),
    [
        ("_on_activated", "Activated", json.dumps(WINDOW)),
        ("_on_target_changed", "TargetChanged", json.dumps(WINDOW)),
        ("_on_popups", "Popups", json.dumps(POPUPS)),
        ("_on_cursor", "Cursor", '{"x": 1, "y": 2}'),
        ("_on_desktop_changed", "DesktopChanged", "{}"),
    ],
)
def test_a_raising_new_callback_does_not_break_the_link(
    harness: Harness, attribute: str, method: str, payload: str, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    def boom(*_args: Any) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("SECRET-FROM-CALLBACK")

    setattr(harness.link, attribute, boom)

    async def scenario() -> None:
        await harness.link.start()
        await harness.link.set_cursor_stream(True)
        with caplog.at_level("DEBUG", logger="waddle"):
            getattr(harness.receiver, method)(payload)  # must not raise
            getattr(harness.receiver, method)(payload)  # and is still delivered next time
        # The rest of the link is unaffected.
        harness.receiver.OwnWindow(json.dumps(OWN_WINDOW))
        assert await harness.link.move_own_window(PENGUIN_WINDOW_TITLE, 1, 2) is True
        assert await harness.link.query_cursor() == CursorPos(640.5, -12)

    run(scenario())
    assert calls >= 2 and len(harness.windows) == 1
    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text


# --- Phase 2: the cursor stream -------------------------------------------------------------


def cursor_calls(bus: "FakeBus") -> list[tuple[str, list[Any]]]:
    return [(member, body) for _, member, body in bus.kwin_calls() if "waddle-cursor" in body]


def test_cursor_stream_is_switched_by_loading_and_unloading_the_script(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.link.start()
        assert cursor_calls(harness.bus) == []  # off until asked for
        harness.receiver.Cursor('{"x": 1, "y": 2}')
        assert harness.cursor == []  # and reports are ignored while it is off

        assert await harness.link.set_cursor_stream(True) is True
        assert harness.bus.kwin_calls()[-3:] == [
            ("/Scripting", "isScriptLoaded", ["waddle-cursor"]),
            ("/Scripting", "loadScript", [str(harness.cursor_script), "waddle-cursor"]),
            ("/Scripting/Script2", "run", []),
        ]
        assert "waddle-cursor" in harness.bus.loaded
        harness.receiver.Cursor('{"x": 3.5, "y": -4}')
        assert harness.cursor == [CursorPos(3.5, -4)]

        before = len(harness.bus.events)
        assert await harness.link.set_cursor_stream(True) is True  # idempotent
        assert len(harness.bus.events) == before

        assert await harness.link.set_cursor_stream(False) is True
        assert harness.bus.kwin_calls()[-1] == ("/Scripting", "unloadScript", ["waddle-cursor"])
        assert "waddle-cursor" not in harness.bus.loaded
        before = len(harness.bus.events)
        assert await harness.link.set_cursor_stream(False) is True  # idempotent
        assert len(harness.bus.events) == before

        harness.receiver.Cursor('{"x": 9, "y": 9}')  # a late report from the unloaded script
        assert harness.cursor == [CursorPos(3.5, -4)]

        assert await harness.link.set_cursor_stream(True) is True  # and on again
        assert [member for member, _ in cursor_calls(harness.bus)].count("loadScript") == 2

    run(scenario())


def test_cursor_stream_unloads_a_stale_script_first(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeBus(stale_cursor=True))

    async def scenario() -> None:
        await harness.link.start()
        assert await harness.link.set_cursor_stream(True) is True

    run(scenario())
    assert [member for member, _ in cursor_calls(harness.bus)] == [
        "isScriptLoaded",
        "unloadScript",
        "loadScript",
    ]


def test_concurrent_cursor_switches_load_the_script_once(harness: Harness) -> None:
    async def scenario() -> list[bool]:
        await harness.link.start()
        return await asyncio.gather(*(harness.link.set_cursor_stream(True) for _ in range(5)))

    assert run(scenario()) == [True] * 5
    assert [member for member, _ in cursor_calls(harness.bus)].count("loadScript") == 1


def test_cursor_stream_fails_cleanly_without_its_script_file(harness: Harness) -> None:
    harness.cursor_script.unlink()

    async def scenario() -> None:
        await harness.link.start()
        assert await harness.link.set_cursor_stream(True) is False
        assert cursor_calls(harness.bus) == []
        # The wish is remembered: once the file is there, asking again loads it.
        harness.cursor_script.write_text("// cursor\n", encoding="utf-8")
        assert await harness.link.set_cursor_stream(True) is True

    run(scenario())
    assert "waddle-cursor" in harness.bus.loaded


def test_cursor_stream_when_kwin_refuses_does_not_leave_a_loaded_script(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.link.start()
        harness.bus.fail_members = {"run"}
        assert await harness.link.set_cursor_stream(True) is False
        assert "waddle-cursor" not in harness.bus.loaded  # loaded but never run: unloaded
        harness.bus.fail_members = set()
        assert await harness.link.set_cursor_stream(True) is True
        harness.bus.kwin = False
        assert await harness.link.set_cursor_stream(False) is False  # KWin is gone
        assert await harness.link.set_cursor_stream(False) is True  # nothing left to do

    run(scenario())


def test_cursor_stream_asked_for_before_start_is_loaded_by_start(harness: Harness) -> None:
    async def scenario() -> None:
        assert await harness.link.set_cursor_stream(True) is False  # not connected yet
        assert await harness.link.start() is True

    run(scenario())
    assert [c[1] for c in harness.bus.kwin_calls()] == [
        "isScriptLoaded",
        "loadScript",
        "run",
        "isScriptLoaded",
        "loadScript",
        "run",
    ]
    assert set(harness.bus.loaded) == {"waddle-core", "waddle-cursor"}


@pytest.mark.parametrize("enabled", [True, False])
def test_cursor_stream_is_reloaded_after_a_kwin_restart_only_if_enabled(
    harness: Harness, enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(link_module, "RELOAD_DELAYS", (0.0, 0.01))

    async def scenario() -> None:
        await harness.link.start()
        await harness.link.set_cursor_stream(True)
        if not enabled:
            await harness.link.set_cursor_stream(False)
        mark = len(harness.bus.events)

        harness.bus.kwin_owner_changed("")  # KWin dies: its runtime scripts die with it
        harness.bus.loaded.clear()
        harness.bus.kwin_owner_changed(":1.99")
        async with asyncio.timeout(2):
            while harness.statuses[-1] != (True, "KWin script reloaded"):
                await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)

        after = [(e[4], e[5]) for e in harness.bus.events[mark:] if e[0] == "call"]
        core_load = ("loadScript", [str(harness.core_script), "waddle-core"])
        cursor_load = ("loadScript", [str(harness.cursor_script), "waddle-cursor"])
        assert after.count(core_load) == 1
        assert after.count(cursor_load) == (1 if enabled else 0)
        assert ("waddle-cursor" in harness.bus.loaded) is enabled
        if enabled:
            assert after.index(core_load) < after.index(cursor_load)
            # Still on as far as the caller is concerned: no second load when asked again.
            before = len(harness.bus.events)
            assert await harness.link.set_cursor_stream(True) is True
            assert len(harness.bus.events) == before
            harness.receiver.Cursor('{"x": 1, "y": 1}')
            assert harness.cursor == [CursorPos(1, 1)]

    run(scenario())


def test_stop_unloads_the_cursor_script_too_and_forgets_the_wish(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.link.start()
        await harness.link.set_cursor_stream(True)
        await harness.link.stop()

    run(scenario())
    assert harness.bus.kwin_calls()[-2:] == [
        ("/Scripting", "unloadScript", ["waddle-cursor"]),
        ("/Scripting", "unloadScript", ["waddle-core"]),
    ]
    assert harness.bus.loaded == {}
    assert harness.link._cursor_wanted is False and harness.link._cursor_loaded is False


# --- Phase 2: one-shot queries --------------------------------------------------------------


def test_query_cursor_runs_a_private_one_shot_script(harness: Harness) -> None:
    async def scenario() -> CursorPos | None:
        await harness.link.start()
        return await harness.link.query_cursor()

    assert run(scenario()) == CursorPos(640.5, -12)

    bus = harness.bus
    [(path, file_mode, dir_mode, text)] = bus.seen_files
    assert path.parent == harness.runtime_dir and (file_mode, dir_mode) == (0o600, 0o700)
    assert '"Cursor"' in text and "cursorPos" in text
    plugin = path.stem
    assert re.fullmatch(r"waddle-query-[0-9a-f]{16}", plugin)
    assert bus.kwin_calls()[-3:] == [
        ("/Scripting", "loadScript", [str(path), plugin]),
        ("/Scripting/Script2", "run", []),
        ("/Scripting", "unloadScript", [plugin]),
    ]
    assert list(harness.runtime_dir.iterdir()) == [] and plugin not in bus.loaded
    assert harness.cursor == []  # the answer is returned, not streamed: the stream is off


def test_window_at_runs_a_private_one_shot_script(harness: Harness) -> None:
    async def scenario() -> WindowAt | None:
        await harness.link.start()
        return await harness.link.window_at(-40, 900)

    assert run(scenario()) == WindowAt(-40, 900, WINDOW_INFO)

    [(path, file_mode, dir_mode, text)] = harness.bus.seen_files
    assert (file_mode, dir_mode) == (0o600, 0o700)
    assert "var X = -40;" in text and "var Y = 900;" in text and '"WindowAt"' in text
    assert re.fullmatch(r"waddle-at-[0-9a-f]{16}", path.stem)
    assert harness.bus.kwin_calls()[-1] == ("/Scripting", "unloadScript", [path.stem])
    assert list(harness.runtime_dir.iterdir()) == [] and path.stem not in harness.bus.loaded
    assert harness.activated == [] and harness.changed == []


def test_window_at_distinguishes_no_window_from_no_answer(tmp_path: Path) -> None:
    nothing = Harness(tmp_path / "a", FakeBus(at_reply=lambda text: window_at_payload(text, None)))
    silent = Harness(tmp_path / "b", FakeBus(at_reply=lambda _text: None))

    async def ask(harness: Harness) -> WindowAt | None:
        await harness.link.start()
        return await harness.link.window_at(5, 6, timeout=0.05)

    assert run(ask(nothing)) == WindowAt(5, 6, None)
    assert run(ask(silent)) is None


@pytest.mark.parametrize(
    "reply",
    [
        None,  # the script never answers
        "garbage",
        json.dumps({"x": 6, "y": 5, "window": None}),  # an answer about another point
        json.dumps({"x": 5, "y": 5, "window": {**WINDOW, "uuid": "nope"}}),
        json.dumps({"x": 5, "y": 5}),
    ],
)
def test_window_at_times_out_and_still_cleans_up(tmp_path: Path, reply: str | None) -> None:
    harness = Harness(tmp_path, FakeBus(at_reply=lambda _text: reply))

    async def scenario() -> WindowAt | None:
        await harness.link.start()
        return await harness.link.window_at(5, 5, timeout=0.05)

    assert run(scenario()) is None
    [(path, _, _, _)] = harness.bus.seen_files
    assert not path.exists() and list(harness.runtime_dir.iterdir()) == []
    assert harness.bus.kwin_calls()[-1] == ("/Scripting", "unloadScript", [path.stem])
    assert harness.link._pending_window_at is None


@pytest.mark.parametrize("reply", [None, "garbage", '{"x": "1", "y": 2}', '{"x": 1e9, "y": 2}'])
def test_query_cursor_times_out_and_still_cleans_up(tmp_path: Path, reply: str | None) -> None:
    harness = Harness(tmp_path, FakeBus(query_reply=lambda _text: reply))

    async def scenario() -> CursorPos | None:
        await harness.link.start()
        return await harness.link.query_cursor(timeout=0.05)

    assert run(scenario()) is None
    [(path, _, _, _)] = harness.bus.seen_files
    assert not path.exists() and list(harness.runtime_dir.iterdir()) == []
    assert harness.bus.kwin_calls()[-1] == ("/Scripting", "unloadScript", [path.stem])
    assert harness.link._pending_cursor is None


def test_a_late_answer_after_a_timeout_is_ignored(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeBus(query_reply=lambda _t: None, at_reply=lambda _t: None))

    async def scenario() -> None:
        await harness.link.start()
        assert await harness.link.query_cursor(timeout=0.02) is None
        assert await harness.link.window_at(1, 2, timeout=0.02) is None
        harness.receiver.Cursor('{"x": 1, "y": 2}')  # nobody is waiting: must not raise
        harness.receiver.WindowAt(json.dumps({"x": 1, "y": 2, "window": None}))
        harness.receiver.MoveDone(json.dumps({"title": "Waddle", "ok": True, "x": 1, "y": 2}))

    run(scenario())
    assert harness.new_callback_counts() == [0, 0, 0, 0, 0]


def test_queries_clean_up_when_kwin_is_gone_and_return_none_before_start(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    async def scenario() -> None:
        assert await harness.link.query_cursor() is None  # before start: nothing is written
        assert await harness.link.window_at(1, 2) is None
        assert not harness.runtime_dir.exists()
        await harness.link.start()
        harness.bus.kwin = False
        assert await harness.link.query_cursor() is None
        assert await harness.link.window_at(1, 2) is None

    run(scenario())
    assert list(harness.runtime_dir.iterdir()) == []


@pytest.mark.parametrize(("x", "y"), [(1.5, 0), ("1", 0), (0, None), (True, 0), (100_001, 0)])
def test_window_at_rejects_bad_coordinates_without_touching_kwin(
    harness: Harness, x: Any, y: Any
) -> None:
    async def scenario() -> None:
        await harness.link.start()
        before = len(harness.bus.events)
        with pytest.raises(ValueError):
            await harness.link.window_at(x, y)
        assert len(harness.bus.events) == before

    run(scenario())
    assert not harness.runtime_dir.exists()


def test_query_cursor_works_while_the_stream_is_on(harness: Harness) -> None:
    async def scenario() -> CursorPos | None:
        await harness.link.start()
        await harness.link.set_cursor_stream(True)
        return await harness.link.query_cursor()

    assert run(scenario()) == CursorPos(640.5, -12)
    assert harness.cursor == [CursorPos(640.5, -12)]  # with the stream on it is also streamed
    assert "waddle-cursor" in harness.bus.loaded  # the one-shot did not unload the stream


def test_queries_run_one_at_a_time_and_each_gets_its_own_answer(harness: Harness) -> None:
    async def scenario() -> list[WindowAt | None]:
        await harness.link.start()
        return await asyncio.gather(*(harness.link.window_at(n, -n) for n in range(4)))

    answers = run(scenario())
    assert [(a.x, a.y) for a in answers if a is not None] == [(n, -n) for n in range(4)]
    assert len({seen[0] for seen in harness.bus.seen_files}) == 4


# --- Phase 2: how the one-shot script file is created ---------------------------------------

ONE_SHOTS: dict[str, Callable[[KWinLink], Coroutine[Any, Any, Any]]] = {
    "move": lambda link: link.move_own_window(PENGUIN_WINDOW_TITLE, 1, 2),
    "query": lambda link: link.query_cursor(),
    "at": lambda link: link.window_at(1, 2),
}


@pytest.fixture(params=sorted(ONE_SHOTS))
def one_shot(
    request: pytest.FixtureRequest, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> tuple[Callable[[], Coroutine[Any, Any, Any]], Path]:
    """One of the three one-shot calls, and the file it will use (the random part pinned)."""
    kind = request.param
    monkeypatch.setattr(link_module, "_random_token", lambda: "pinned")

    async def call() -> Any:
        await harness.link.start()
        return await ONE_SHOTS[kind](harness.link)

    return call, harness.runtime_dir / f"waddle-{kind}-pinned.js"


def one_shot_loads(harness: Harness) -> list[list[Any]]:
    return [body for _, member, body in harness.bus.kwin_calls() if member == "loadScript"][1:]


def test_one_shot_uses_the_pinned_name_when_nothing_is_in_the_way(
    harness: Harness, one_shot: tuple[Callable[[], Coroutine[Any, Any, Any]], Path]
) -> None:
    call, path = one_shot
    assert run(call()) not in (None, False)
    assert one_shot_loads(harness) == [[str(path), path.stem]]  # so the tests below bite
    assert not path.exists()


def test_one_shot_fails_on_a_file_that_is_already_there_and_leaves_it(
    harness: Harness, one_shot: tuple[Callable[[], Coroutine[Any, Any, Any]], Path]
) -> None:
    call, path = one_shot
    path.parent.mkdir(parents=True)
    path.write_text("// planted\n", encoding="utf-8")
    path.chmod(0o644)

    assert run(call()) in (None, False)

    assert path.read_text(encoding="utf-8") == "// planted\n"  # not overwritten, not deleted
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert one_shot_loads(harness) == []  # and KWin was never asked to run it
    assert path.stem not in json.dumps(harness.bus.kwin_calls())


@pytest.mark.parametrize("dangling", [False, True])
def test_one_shot_fails_on_a_symlink_that_is_already_there_and_leaves_it(
    harness: Harness,
    one_shot: tuple[Callable[[], Coroutine[Any, Any, Any]], Path],
    tmp_path: Path,
    dangling: bool,
) -> None:
    call, path = one_shot
    victim = tmp_path / "victim.txt"
    if not dangling:
        victim.write_text("precious\n", encoding="utf-8")
    path.parent.mkdir(parents=True)
    path.symlink_to(victim)

    assert run(call()) in (None, False)

    assert path.is_symlink() and path.readlink() == victim  # the link is untouched
    if dangling:
        assert not victim.exists()  # nothing was written through it
    else:
        assert victim.read_text(encoding="utf-8") == "precious\n"
    assert one_shot_loads(harness) == []


def test_one_shot_tightens_a_loose_runtime_dir_and_writes_a_private_file(
    harness: Harness, one_shot: tuple[Callable[[], Coroutine[Any, Any, Any]], Path]
) -> None:
    call, path = one_shot
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o777)
    old_umask = os.umask(0)  # even with the most permissive umask
    try:
        assert run(call()) not in (None, False)
    finally:
        os.umask(old_umask)

    [(seen_path, file_mode, dir_mode, _)] = harness.bus.seen_files
    assert seen_path == path and (file_mode, dir_mode) == (0o600, 0o700)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.iterdir()) == []


def test_one_shot_names_are_unpredictable_and_distinct() -> None:
    tokens = {link_module._random_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(re.fullmatch(r"[0-9a-f]{16}", token) for token in tokens)  # 64 random bits


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
            self.scripting.call_back("Hello", '{"version": 2, "apiOk": true}')
            self.scripting.call_back("Outputs", json.dumps(OUTPUTS))
            self.scripting.call_back("OwnWindow", json.dumps(OWN_WINDOW))
            self.scripting.call_back("OwnWindow", "not json")
            self.scripting.call_back("Activated", json.dumps(WINDOW))
            self.scripting.call_back("Activated", json.dumps({**WINDOW, "uuid": "bad"}))
            self.scripting.call_back("TargetChanged", json.dumps({**WINDOW, "fullscreen": True}))
            self.scripting.call_back("Popups", json.dumps(POPUPS))
            self.scripting.call_back("DesktopChanged", "{}")
        elif self.plugin == "waddle-cursor":
            self.scripting.call_back("Cursor", '{"x": 7, "y": 8}')
        elif self.plugin.startswith("waddle-move-"):
            source = Path(self.file).read_text(encoding="utf-8")
            self.scripting.call_back("MoveDone", move_done_payload(source))
        elif self.plugin.startswith("waddle-query-"):
            self.scripting.call_back("Cursor", cursor_payload(""))
        elif self.plugin.startswith("waddle-at-"):
            source = Path(self.file).read_text(encoding="utf-8")
            self.scripting.call_back("WindowAt", window_at_payload(source))


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

            # The Phase 2 reports of the core script arrived over the real bus, parsed.
            await until(lambda: harness.desktop_changes == 1)
            assert harness.activated == [WINDOW_INFO]  # the malformed one was dropped
            assert [w.fullscreen for w in harness.changed] == [True]
            assert [p.target for p in harness.popups] == [UUID_A]

            # One-shot queries.
            assert await harness.link.query_cursor() == CursorPos(640.5, -12)
            assert await harness.link.window_at(3, 4) == WindowAt(3, 4, WINDOW_INFO)
            assert [entry.rsplit("-", 1)[0] for entry in kwin.log[3:]] == [
                "load:waddle-query",
                "unload:waddle-query",
                "load:waddle-at",
                "unload:waddle-at",
            ]
            assert list(harness.runtime_dir.iterdir()) == []
            assert harness.cursor == []  # the stream is off

            # The cursor stream: loading the script is the switch.
            assert await harness.link.set_cursor_stream(True) is True
            assert kwin.log[-1] == "load:waddle-cursor"
            await until(lambda: harness.cursor == [CursorPos(7, 8)])

            # KWin restarts: its runtime scripts are gone, the link loads both again.
            await kwin_bus.release_name("org.kde.KWin")
            await until(lambda: harness.statuses[-1] == (False, "KWin is not running"))
            kwin.loaded.clear()
            await kwin_bus.request_name("org.kde.KWin")
            await until(lambda: len(harness.windows) == 2 and harness.statuses[-1][0])
            await until(lambda: len(harness.cursor) == 2)
            assert kwin.log[-2:] == ["load:waddle-core", "load:waddle-cursor"]

            assert await harness.link.set_cursor_stream(False) is True
            assert kwin.log[-1] == "unload:waddle-cursor"
            await harness.link.stop()
            assert kwin.log[-1] == "unload:waddle-core"
            assert kwin.loaded == {}
        finally:
            await harness.link.stop()
            await asyncio.gather(*kwin.tasks, return_exceptions=True)
            kwin_bus.disconnect()

    run(scenario())
