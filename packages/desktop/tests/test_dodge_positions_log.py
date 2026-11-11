import json
from pathlib import Path

from waddle_desktop import log
from waddle_desktop.placement.positions import (
    PositionStore,
    Rect,
    clamp_to_output,
    default_position,
)
from waddle_desktop.ui.dodge import DodgeMachine, DodgeState


def machine() -> DodgeMachine:
    return DodgeMachine(dwell_ms=300, return_ms=1000)


def test_dodges_after_dwell_and_returns_on_timer_not_on_leave() -> None:
    m = machine()
    m.hover_enter(0)
    m.tick(299)
    assert not m.dodged
    m.tick(300)
    assert m.dodged and m.deadline == 1300
    m.hover_leave(400)  # leaving must not bring him back: that would oscillate
    m.hover_enter(450)
    assert m.dodged
    m.tick(1300)
    assert m.state is DodgeState.NORMAL and m.deadline is None


def test_press_or_leave_during_dwell_cancels() -> None:
    for cancel in ("press", "hover_leave"):
        m = machine()
        m.hover_enter(0)
        getattr(m, cancel)(100)
        m.tick(1000)
        assert not m.dodged and m.deadline is None


def test_never_dodges_while_bubble_open_or_disabled() -> None:
    m = machine()
    m.set_blocked(True, 0)
    m.hover_enter(0)
    m.tick(1000)
    assert not m.dodged
    m.set_blocked(False, 1000)
    m.hover_enter(1000)
    m.tick(1300)
    assert m.dodged
    m.set_blocked(True, 1400)  # opening the bubble brings him straight back
    assert not m.dodged

    off = DodgeMachine(enabled=False)
    off.hover_enter(0)
    off.tick(10_000)
    assert not off.dodged


def test_default_and_clamped_positions() -> None:
    second = Rect(1920, -200, 2560, 1440)  # right of the primary, negative origin
    assert default_position(second, (400, 560), 24) == (1920 + 2560 - 424, -200 + 1440 - 584)
    assert clamp_to_output((9999, 9999), second, (400, 560)) == (4480 - 400, 1240 - 560)
    assert clamp_to_output((0, -999), second, (400, 560)) == (1920, -200)
    tiny = Rect(0, 0, 300, 300)  # window larger than the output: pin to the origin
    assert default_position(tiny, (400, 560), 24) == (0, 0)


def test_positions_are_per_output_and_survive_layout_changes(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "desktop.db"
    store = PositionStore(db)
    left, right = Rect(0, 0, 1920, 1080), Rect(1920, 0, 2560, 1440)
    assert store.load(left, "DP-1") is None
    store.save(left, "DP-1", (100, 200))
    store.save(right, "HDMI-1", (2000, 300))
    store.save(left, "DP-1", (150, 250))
    store.close()

    store = PositionStore(db)
    assert store.load(left, "DP-1") == (150, 250)
    moved_right = Rect(0, 1080, 2560, 1440)  # the user rearranged monitors
    assert store.load(moved_right, "HDMI-1") == (80, 1380)
    assert oct(db.stat().st_mode & 0o777) == "0o600"
    store.forget()
    assert store.load(left, "DP-1") is None


def test_log_redacts_everything_that_is_not_an_identifier_or_number() -> None:
    line = json.loads(
        log.render(
            "window.activated",
            {"ms": 12.5, "ok": True, "app": "org.kde.konsole", "title": "secret — Bank of X"},
        )
    )
    assert line["ms"] == 12.5 and line["ok"] is True and line["app"] == "org.kde.konsole"
    assert line["title"] == log.REDACTED
    assert "Bank" not in json.dumps(line)
    assert json.loads(log.render("x", {"path": "/home/me/file.txt"}))["path"] == log.REDACTED
