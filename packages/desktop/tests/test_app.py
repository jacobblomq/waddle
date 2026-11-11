"""WaddleApp itself: placement memory, dodge wiring, instance lock, shutdown."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from waddle_core.config import PlacementConfig, WaddleConfig
from waddle_core.identity import OVERLAY_WINDOW_TITLE, PENGUIN_WINDOW_TITLE
from waddle_core.paths import WaddlePaths, resolve_paths
from waddle_desktop.cli import main as cli
from waddle_desktop.kwin.types import OutputInfo, OwnWindow
from waddle_desktop.ui import app as app_module
from waddle_desktop.ui.app import WaddleApp, acquire_instance_lock

OUTPUT = OutputInfo("DP-1", 0, 0, 2560, 1440)
SIZE = (400, 600)
DEFAULT = (2560 - 400 - 24, 1440 - 600 - 24)


def own(x: int, y: int, uuid: str = "u1", title: str = PENGUIN_WINDOW_TITLE) -> OwnWindow:
    return OwnWindow(title, uuid, x, y, SIZE[0], SIZE[1], "DP-1")


@pytest.fixture
def paths(tmp_path: Path) -> WaddlePaths:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "c"),
        "XDG_DATA_HOME": str(tmp_path / "d"),
        "XDG_CACHE_HOME": str(tmp_path / "k"),
        "XDG_RUNTIME_DIR": str(tmp_path / "r"),
    }
    resolved = resolve_paths(env, tmp_path)
    for directory in resolved.all_dirs():
        directory.mkdir(parents=True)
    return resolved


class Harness:
    def __init__(self, paths: WaddlePaths, config: WaddleConfig | None = None) -> None:
        self.app = WaddleApp(config or WaddleConfig(), paths)
        self.moves: list[tuple[Any, ...]] = []
        kwin = self.app._kwin

        async def fake_move(title: str, x: int, y: int) -> bool:
            return True

        def record(title: str, x: int, y: int) -> Any:
            self.moves.append((title, x, y))
            return fake_move(title, x, y)

        kwin.move_own_window = record  # type: ignore[method-assign]
        self.app._io.submit = lambda coro: coro.close()  # type: ignore[method-assign,assignment]

    def outputs(self) -> None:
        self.app._on_outputs([OUTPUT], "DP-1")

    def report(self, window: OwnWindow) -> None:
        self.app._on_own_window(window)

    def saved(self) -> tuple[int, int] | None:
        self.app._flush_position()
        from waddle_desktop.placement.positions import Rect

        return self.app._positions.load(Rect(0, 0, 2560, 1440), "DP-1")


@pytest.fixture
def harness(qtbot: Any, paths: WaddlePaths) -> Iterator[Harness]:
    h = Harness(paths)
    yield h
    h.app.shutdown()


def test_first_run_moves_to_bottom_right_and_does_not_remember_it(harness: Harness) -> None:
    harness.outputs()
    harness.report(own(300, 200))  # where KWin happened to put the new window
    assert harness.moves == [(PENGUIN_WINDOW_TITLE, *DEFAULT)]
    harness.report(own(*DEFAULT))  # the echo of our own move
    assert harness.saved() is None, "the default spot must not be stored as the user's choice"


def test_a_drag_is_remembered_once_it_settles(harness: Harness) -> None:
    harness.outputs()
    harness.report(own(300, 200))
    harness.report(own(*DEFAULT))
    for x in (500, 400, 300, 100):  # KWin reports every step of the drag
        harness.report(own(x, 100))
    assert harness.app._positions.load(_rect(), "DP-1") is None, "no write per drag step"
    assert harness.saved() == (100, 100)
    assert len(harness.moves) == 1, "a drag must never trigger a counter-move"


def test_a_new_surface_is_put_back_instead_of_overwriting_the_saved_spot(harness: Harness) -> None:
    harness.outputs()
    harness.report(own(300, 200))
    harness.report(own(*DEFAULT))
    harness.report(own(100, 100))
    assert harness.saved() == (100, 100)
    # Hide/Show (or a KWin restart) creates a new window that KWin places somewhere else.
    harness.report(own(800, 400, uuid="u2"))
    assert harness.moves[-1] == (PENGUIN_WINDOW_TITLE, 100, 100)
    harness.report(own(100, 100, uuid="u2"))
    assert harness.saved() == (100, 100)


def test_saved_spot_is_restored_on_the_next_start(qtbot: Any, paths: WaddlePaths) -> None:
    first = Harness(paths)
    first.outputs()
    first.report(own(300, 200))
    first.report(own(*DEFAULT))
    first.report(own(640, 480))
    first.app.shutdown()  # must flush the pending save

    second = Harness(paths)
    second.report(own(10, 10, uuid="new"))  # geometry may arrive before the outputs
    assert second.moves == []
    second.outputs()
    assert second.moves == [(PENGUIN_WINDOW_TITLE, 640, 480)]
    second.app.shutdown()


def test_the_overlay_and_unknown_outputs_never_touch_position_memory(harness: Harness) -> None:
    harness.outputs()
    harness.report(own(1, 2, title=OVERLAY_WINDOW_TITLE))
    assert harness.moves == [] and harness.saved() is None
    harness.report(own(300, 200))
    harness.report(own(*DEFAULT))
    harness.report(OwnWindow(PENGUIN_WINDOW_TITLE, "u1", 5, 5, 400, 600, "UNKNOWN-9"))
    assert harness.saved() is None


def test_dodge_is_wired_through_the_bridge(qtbot: Any, paths: WaddlePaths) -> None:
    config = WaddleConfig(placement=PlacementConfig(dodge_dwell_ms=40, dodge_return_ms=500))
    h = Harness(paths, config)
    bridge = h.app._bridge
    try:
        bridge.bodyHover(True)
        assert bridge.property("dodged") is False
        qtbot.waitUntil(lambda: bridge.property("dodged") is True, timeout=1000)
        qtbot.waitUntil(lambda: bridge.property("dodged") is False, timeout=2000)

        bridge.bodyHover(False)
        bridge.bodyHover(True)
        bridge.bodyPress()  # a press during the dwell is a click or a drag, not a reason to move
        qtbot.wait(120)
        assert bridge.property("dodged") is False

        bridge.set_bubble("hello")
        bridge.bodyHover(False)
        bridge.bodyHover(True)
        qtbot.wait(120)
        assert bridge.property("dodged") is False, "never dodge while a bubble is open"
    finally:
        h.app.shutdown()


def test_pause_shows_sleeping_and_updates_the_tray(harness: Harness) -> None:
    harness.app._tray.pause.trigger()
    assert harness.app._bridge.current_mood.value == "sleeping"
    assert harness.app._tray.state.value == "paused"
    harness.app._tray.pause.trigger()
    assert harness.app._bridge.current_mood.value == "idle"


def test_ask_window_submits_text_and_closes(harness: Harness, qtbot: Any) -> None:
    texts: list[str] = []
    harness.app._bridge.textSubmitted.connect(texts.append)
    harness.app.open_ask()
    ask = harness.app._ask
    assert ask is not None and ask.isVisible()
    root = ask.rootObject()
    assert root is not None
    root.submitted.emit("  how do I render?  ")  # type: ignore[attr-defined]
    assert texts == ["how do I render?"] and not ask.isVisible()
    harness.app.open_ask()
    root.cancelled.emit()  # type: ignore[attr-defined]
    assert not ask.isVisible() and len(texts) == 1


def test_only_one_instance_and_stop_never_trusts_a_stale_pid_file(
    paths: WaddlePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(cli, "_paths", lambda: paths)

    # A stale file naming a live process (this test runner; its command line even contains
    # "waddle") must be ignored: nobody holds the lock.
    paths.pid_file.write_text(str(os.getpid()))
    assert cli.running_pid(paths) is None
    assert cli.main(["stop"]) == 0 and killed == []

    lock = acquire_instance_lock(paths)
    assert lock is not None
    assert cli.running_pid(paths) == os.getpid()
    # A second start in another process would block on the same lock:
    probe = os.open(paths.pid_file, os.O_RDWR)
    with pytest.raises(BlockingIOError):
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(probe)
    assert cli.main(["stop"]) == 0 and killed == [os.getpid()]
    os.close(lock)
    assert cli.running_pid(paths) is None


def test_run_refuses_to_start_twice(paths: WaddlePaths, capsys: pytest.CaptureFixture[str]) -> None:
    lock = acquire_instance_lock(paths)
    assert lock is not None
    holder = os.open(paths.pid_file, os.O_RDWR)  # a different open file description
    os.close(holder)
    # Simulate "another process holds it": flock is per open file description.
    assert app_module.acquire_instance_lock(paths) is None
    assert app_module.run(WaddleConfig(), paths) == 0
    assert "already running" in capsys.readouterr().out
    os.close(lock)


def _rect() -> Any:
    from waddle_desktop.placement.positions import Rect

    return Rect(0, 0, 2560, 1440)
