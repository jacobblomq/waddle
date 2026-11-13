"""Phase 2 glue: helper start-up checks, the eyes panel, hotkey planning, doctor, app wiring."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSize

from waddle_core.config import HotkeysConfig, WaddleConfig, WatchingConfig
from waddle_core.mood import Mood
from waddle_core.paths import WaddlePaths, resolve_paths
from waddle_core.privacy import BlindReason
from waddle_core.window import Geometry, WindowInfo
from waddle_desktop.cli import doctor
from waddle_desktop.identity.cache import UNKNOWN_IDENTITY
from waddle_desktop.identity.packaging import Packaging
from waddle_desktop.identity.version import Version
from waddle_desktop.sensors import hotkey_setup
from waddle_desktop.sensors.capture_client import Frame, Hello
from waddle_desktop.sensors.helper_proc import HelperUnavailableError, check_binary, start_helper
from waddle_desktop.sensors.hotkey_setup import HotkeyPlan, make_plan
from waddle_desktop.sensors.hotkeys import BindingPlan, Conflict
from waddle_desktop.sensors.keys import parse
from waddle_desktop.sensors.senses import EyesState, HelperEye, Look
from waddle_desktop.sensors.session import SessionSnapshot
from waddle_desktop.ui.app import WaddleApp
from waddle_desktop.ui.eyes import EyesModel, FrameProvider, eyes_rows

REPO = Path(__file__).resolve().parents[3]
BUILT = REPO / "capture-helper" / "target" / "release" / "waddle-capture"
G = Geometry(0, 0, 1280, 720)
UNLOCKED = SessionSnapshot(locked=False, lock_known=True, dnd=False, idle=False)


def win(*, fullscreen: bool = False, cls: str = "blender") -> WindowInfo:
    uuid = "11111111-1111-1111-1111-111111111111"
    return WindowInfo(
        uuid, "Secret title", cls, cls, cls, 10, G, G, "DP-1", 1.25, fullscreen, False, False
    )


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


# ---- starting the helper ---------------------------------------------------------------


def test_helper_binary_checks(tmp_path: Path) -> None:
    def reason(path: Path) -> str:
        with pytest.raises(HelperUnavailableError) as caught:
            check_binary(path)
        return str(caught.value)

    assert reason(Path("relative/waddle-capture")) == "not_absolute"
    assert reason(tmp_path / "nope") == "missing"
    assert reason(tmp_path) == "not_a_file"
    target = tmp_path / "real"
    target.write_bytes(b"#!/bin/true\n")
    target.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    assert reason(link) == "not_a_file", "a symlink is never followed"
    target.chmod(0o600)
    assert reason(target) == "not_executable"
    target.chmod(0o722)
    assert reason(target) == "unsafe_permissions"
    target.chmod(0o700)
    check_binary(target)


@pytest.mark.skipif(not BUILT.is_file(), reason="waddle-capture is not built")
def test_the_real_helper_starts_answers_and_exits(tmp_path: Path) -> None:
    binary = tmp_path / "waddle-capture"
    binary.write_bytes(BUILT.read_bytes())
    binary.chmod(0o700)

    async def scenario() -> tuple[Hello, Any]:
        process = start_helper(binary)
        eye = HelperEye(process)
        try:
            return await eye.hello(), process.process
        finally:
            eye.close()

    hello, process = asyncio.run(scenario())
    assert hello.protocol == 1
    assert process.poll() == 0, "closing our end of the socket ends the helper"


# ---- the eyes panel --------------------------------------------------------------------


def rows(state: EyesState) -> dict[str, str]:
    return {r["label"]: r["value"] for r in eyes_rows(state, now=100.0)}


def test_rows_describe_the_app_without_ever_showing_the_title() -> None:
    identity = replace(
        UNKNOWN_IDENTITY, name="blender", version=Version(5, 3, 1), packaging=Packaging.FLATPAK
    )
    state = EyesState(
        blind=None,
        window=win(),
        identity=identity,
        identify_ms=12.4,
        session=UNLOCKED,
        helper=Hello("0.2.0", 1, True, 4, "restricted", True, True, False),
        look=Look("ok", at=97.0, width=1600, height=900, elapsed_ms=41, popups=1),
    )
    got = rows(state)
    assert got["May I look?"] == "Yes"
    assert got["App"] == "blender · 5.3.1 · flatpak"
    assert got["Named in"].startswith("12 ms")
    assert "1280 x 720 at 1.25x" in got["Window"] and "Wayland" in got["Window"]
    assert "ScreenShot2 v4" in got["Capture path"]
    assert got["Last look"] == "1600 x 900 px in 41 ms, 1 popup(s), 3 s ago"
    assert got["Session"] == "unlocked, Do Not Disturb off"
    assert "Secret title" not in repr(eyes_rows(state))


@pytest.mark.parametrize("reason", list(BlindReason))
def test_every_blind_reason_has_plain_words(reason: BlindReason) -> None:
    text = rows(EyesState(blind=reason, window=win(), session=UNLOCKED))["May I look?"]
    assert text and text != "Yes"


def test_capture_errors_come_with_what_to_do() -> None:
    state = EyesState(blind=None, window=win(), session=UNLOCKED)
    for code in ("not_authorized", "helper-missing", "config_unreadable", "lock_unknown"):
        text = rows(replace(state, look=Look("error", code, at=99.0)))["Last look"]
        assert code in text and len(text) > len(code) + 20


def test_frames_live_in_memory_and_are_dropped_on_request(qtbot: Any) -> None:
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage

    image = QImage(8, 6, QImage.Format.Format_RGB32)
    image.fill(0xFF336699)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    png = bytes(buffer.data().data())

    provider = FrameProvider()
    model = EyesModel(provider)
    assert not model.property("hasFrame")
    model.set_frame(Frame("0" * 16, 1.0, 8, 6, png, (8, 6), png, (0, 0, 8, 6), 3))
    assert model.property("hasFrame") and model.property("revision") == 1
    size = QSize()
    got = provider.requestImage("overview?1", size, QSize())
    assert (got.width(), got.height(), size.width()) == (8, 6, 8)
    model.set_frame(None)
    assert not model.property("hasFrame")
    assert provider.requestImage("overview?2", QSize(), QSize()).width() == 1, "nothing is left"


def test_eyes_qml_loads_and_reflects_the_model(qtbot: Any, load_qml: Any) -> None:
    from PySide6.QtCore import QObject

    model = EyesModel(FrameProvider())
    root = load_qml("Eyes.qml", eyes=model)
    button = root.findChild(QObject, "lookNow")
    status = root.findChild(QObject, "status")
    assert not button.property("enabled") and "Eyes closed" in status.property("text")
    asked: list[bool] = []
    model.lookRequested.connect(lambda: asked.append(True))
    model.set_state(EyesState(blind=None, window=win(), session=UNLOCKED))
    assert button.property("enabled") and "No picture yet" in status.property("text")
    button.clicked.emit()
    assert asked == [True]


# ---- hotkeys ---------------------------------------------------------------------------


def test_a_taken_or_unchecked_key_is_never_requested() -> None:
    free, taken = parse("Meta+Shift+W"), parse("Meta+Space")
    plans = (
        BindingPlan("pause", free, (), None, free),
        BindingPlan("ask", None, (parse("Meta+Alt+Space"),), Conflict("KRunner", "Run"), taken),
    )
    assert HotkeyPlan(plans, {"quiet": "bad"}, True).bindings() == {
        "pause": free,
        "ask": None,
        "quiet": None,
    }
    assert set(HotkeyPlan(plans, {}, False).bindings().values()) == {None}


def test_only_actions_that_do_something_are_planned_and_no_bus_means_unchecked() -> None:
    async def no_bus() -> Any:
        raise OSError("no session bus")

    plan = asyncio.run(make_plan(HotkeysConfig(), no_bus))
    assert sorted(p.action for p in plan.plans) == sorted(hotkey_setup.LIVE_ACTIONS)
    assert not plan.conflicts_checked
    assert "push_to_talk" not in plan.bindings()


# ---- doctor ----------------------------------------------------------------------------


def test_doctor_reports_problems_with_fixes_and_never_raises(paths: WaddlePaths) -> None:
    env = {"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "GNOME"}
    checks = asyncio.run(doctor.collect(paths, env))
    by_text = {c.text: c for c in checks}
    assert by_text["session type is 'x11'"].level == doctor.BAD
    assert by_text["capture helper is not installed"].fix
    assert by_text["default private list loads"].level == doctor.OK
    text = doctor.render(checks)
    assert "✗ capture helper is not installed" in text and "problem(s)" in text


def test_doctor_checks_the_helper_desktop_entry(paths: WaddlePaths) -> None:
    helper = paths.capture_bin
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"x")
    apps = paths.data_dir.parent / "applications"
    apps.mkdir()
    entry = apps / "io.github.jacobblomq.Waddle.Capture.desktop"
    entry.write_text("[Desktop Entry]\nExec=/somewhere/else\n")
    texts = [c.text for c in doctor.check_files(paths, paths.data_dir.parent, REPO)]
    assert "helper's desktop entry is missing or points elsewhere" in texts
    entry.write_text(
        f"[Desktop Entry]\nExec={helper}\n"
        "X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2\n"
    )
    texts = [c.text for c in doctor.check_files(paths, paths.data_dir.parent, REPO)]
    assert "helper's desktop entry names the installed binary" in texts


def test_doctor_flags_a_broken_config(paths: WaddlePaths) -> None:
    paths.config_file.write_text("[privacy\n")
    config, checks = doctor.check_config(paths)
    assert config is None and checks[0].level == doctor.BAD


# ---- the app ---------------------------------------------------------------------------


@pytest.fixture
def app(qtbot: Any, paths: WaddlePaths) -> Any:
    made: list[WaddleApp] = []

    def make(config: WaddleConfig | None = None) -> WaddleApp:
        waddle = WaddleApp(config or WaddleConfig(), paths)
        made.append(waddle)
        return waddle

    yield make
    for waddle in made:
        waddle.shutdown()


def test_the_penguin_shows_what_the_gate_says(app: Any) -> None:
    waddle = app()
    mood = lambda: waddle._bridge.current_mood  # noqa: E731
    assert mood() is Mood.IDLE, "before the sensors report, he does not act blind"
    state = EyesState(blind=BlindReason.BLOCKED, window=win(), session=UNLOCKED)
    waddle._on_eyes(state)
    assert mood() is Mood.EYES_CLOSED
    waddle._on_eyes(replace(state, blind=BlindReason.UNKNOWN_LOCK))
    assert mood() is Mood.EYES_CLOSED
    waddle._on_eyes(replace(state, blind=BlindReason.LOCKED))
    assert mood() is Mood.SLEEPING
    waddle._on_eyes(replace(state, blind=None))
    assert mood() is Mood.IDLE
    waddle._on_eyes(replace(state, blind=BlindReason.NO_WINDOW, window=None))
    assert mood() is Mood.IDLE, "an empty desktop is not a privacy state"


def test_fullscreen_badge_hide_stay_and_bring_back(app: Any) -> None:
    full = EyesState(blind=None, window=win(fullscreen=True), session=UNLOCKED)
    normal = replace(full, window=win())

    badge = app()
    badge._view.show()
    badge._on_eyes(full)
    assert badge._bridge.property("badge") and badge._view.isVisible()
    badge._on_hotkey("bring_back", True)
    assert not badge._bridge.property("badge")
    badge._on_eyes(normal)
    badge._on_eyes(full)
    assert badge._bridge.property("badge"), "bring-back lasts until fullscreen ends"

    hide = app(WaddleConfig(watching=WatchingConfig(fullscreen="hide")))
    hide._view.show()
    hide._on_eyes(full)
    assert not hide._view.isVisible()
    hide._on_hotkey("bring_back", True)
    assert hide._view.isVisible()
    hide._on_eyes(normal)
    assert hide._view.isVisible()

    stay = app(WaddleConfig(watching=WatchingConfig(fullscreen="stay")))
    stay._view.show()
    stay._on_eyes(full)
    assert stay._view.isVisible() and not stay._bridge.property("badge")


def test_hiding_from_the_tray_wins_over_everything(app: Any) -> None:
    waddle = app()
    waddle._view.show()
    waddle._tray.show_waddle.setChecked(False)
    assert not waddle._view.isVisible()
    waddle._on_eyes(EyesState(blind=None, window=win(), session=UNLOCKED))
    assert not waddle._view.isVisible()
    waddle._on_hotkey("bring_back", True)
    assert waddle._view.isVisible() and waddle._tray.show_waddle.isChecked()


def test_hotkeys_act_on_press_only(app: Any) -> None:
    waddle = app()
    waddle._on_hotkey("pause", False)
    assert not waddle._paused
    waddle._on_hotkey("pause", True)
    assert waddle._paused and waddle._bridge.current_mood is Mood.SLEEPING
    waddle._on_hotkey("pause", True)
    assert not waddle._paused
    waddle._on_hotkey("quiet", True)
    assert waddle._quiet
    waddle._on_hotkey("ask", True)
    assert waddle._ask is not None and waddle._ask.isVisible()
    waddle._on_hotkey("push_to_talk", True)  # not live yet: ignored, never an error


def test_unsolicited_bubbles_pass_the_proactivity_gate(app: Any) -> None:
    waddle = app()
    assert waddle._may_volunteer("demo-greeting")
    waddle._set_quiet(True)
    assert not waddle._may_volunteer("demo-greeting")
    waddle._set_quiet(False)
    waddle._on_eyes(EyesState(blind=None, session=replace(UNLOCKED, dnd=True)))
    assert not waddle._may_volunteer("demo-greeting"), "Do Not Disturb silences tips"


def test_frames_are_kept_only_while_the_panel_is_open(app: Any) -> None:
    waddle = app()
    frame = Frame("0" * 16, 1.0, 8, 6, b"x", (8, 6), b"x", (0, 0, 8, 6), 3)
    waddle._on_frame(frame)
    assert not waddle._frames.has_frame, "panel closed: the frame is not kept"
    waddle.open_eyes()
    waddle._on_frame(frame)
    assert waddle._frames.has_frame
    assert waddle._eyes_view is not None
    waddle._eyes_view.hide()
    assert not waddle._frames.has_frame


def test_no_frame_or_capture_file_is_ever_written(app: Any, paths: WaddlePaths) -> None:
    waddle = app()
    waddle.open_eyes()
    waddle._on_frame(Frame("0" * 16, 1.0, 8, 6, b"JPEGDATA", (8, 6), b"PNGDATA", (0, 0, 8, 6), 3))
    waddle._on_eyes(EyesState(blind=None, window=win(), session=UNLOCKED))
    written = [p for d in paths.all_dirs() for p in d.rglob("*") if p.is_file()]
    for path in written:
        data = path.read_bytes()
        assert b"JPEGDATA" not in data and b"PNGDATA" not in data
        assert b"Secret title" not in data


# ---- fixes after the first run on a real desktop ----------------------------------------


def test_the_window_rule_puts_waddle_above_fullscreen_windows() -> None:
    from waddle_desktop.placement.kwin_rules import desired_rules

    for spec in desired_rules(None):
        entries = spec.entries()
        assert (entries["layer"], entries["layerrule"]) == ("critical-notification", "2")


def test_the_tray_shows_the_key_that_is_really_bound(qtbot: Any) -> None:
    from waddle_desktop.sensors.hotkey_setup import shortcut_labels
    from waddle_desktop.sensors.hotkeys import BindResult, BindStatus
    from waddle_desktop.ui.tray import Tray

    key = parse("Meta+Shift+W")
    plan = HotkeyPlan(
        (BindingPlan("pause", key, (), None, key), BindingPlan("ask", None, (), None, key)),
        {},
        True,
    )
    bound = BindResult(BindStatus.BOUND, ("pause", "ask"), (), {"ask": "Meta+A"})
    labels = shortcut_labels(plan, bound)
    assert labels == {"pause": "Meta+Shift+W", "ask": "Meta+A"}, "the portal's answer wins"
    assert shortcut_labels(plan, None) == {}
    assert shortcut_labels(plan, BindResult(BindStatus.CANCELLED)) == {}

    tray = Tray("#3daee9")
    tray.set_shortcuts(labels)
    assert tray.pause.text() == "Pause\tMeta+Shift+W"
    assert tray.ask.text() == "Ask Waddle…\tMeta+A"
    assert tray.quiet.text() == "Quiet mode"
    tray.set_shortcuts({})
    assert tray.pause.text() == "Pause"
    asked: list[bool] = []
    tray.askRequested.connect(lambda: asked.append(True))
    tray.ask.trigger()
    assert asked == [True]
