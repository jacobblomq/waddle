"""The assembled penguin window: bridge + Main.qml + demo driver + tray."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QtMsgType, QUrl, qInstallMessageHandler
from PySide6.QtTest import QTest

from waddle_core.guide import mock_guide
from waddle_core.mood import Mood
from waddle_desktop.theme.palette import FALLBACK_LIGHT
from waddle_desktop.ui.app import QML_DIR, PenguinView
from waddle_desktop.ui.bridge import Bridge
from waddle_desktop.ui.demo import DemoDriver, advance
from waddle_desktop.ui.theme_object import ThemeObject
from waddle_desktop.ui.tray import Tray, TrayState, paint_icon

# The offscreen platform cannot set masks and says so on every call; everything else fails.
_EXPECTED = ("This plugin does not support setting window masks",)


@pytest.fixture
def qt_messages() -> Iterator[list[str]]:
    seen: list[str] = []

    def handler(kind: QtMsgType, _context: Any, message: str) -> None:
        if kind != QtMsgType.QtDebugMsg and not message.startswith(_EXPECTED):
            seen.append(message)

    previous = qInstallMessageHandler(handler)
    yield seen
    qInstallMessageHandler(previous)
    assert seen == [], f"unexpected Qt messages: {seen}"


class Shell:
    def __init__(self, qtbot: Any) -> None:
        self.bridge = Bridge(ThemeObject(FALLBACK_LIGHT))
        self.bridge.set_view(motion_scale=0.0)
        self.rects: list[list[QRect]] = []
        self.bridge.inputRectsChanged.connect(self.rects.append)
        self.view = PenguinView(self.bridge)
        self.view.setSource(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
        assert not self.view.errors(), [e.toString() for e in self.view.errors()]
        self.view.show()
        qtbot.waitExposed(self.view)
        qtbot.waitUntil(lambda: bool(self.rects), timeout=2000)
        self.qtbot = qtbot

    def settle(self) -> list[QRect]:
        """Wait for the next published input mask and return it."""
        count = len(self.rects)
        self.qtbot.waitUntil(lambda: len(self.rects) > count, timeout=2000)
        self.qtbot.wait(30)
        return self.rects[-1]

    @property
    def penguin(self) -> QRect:
        return self.rects[-1][0]


@pytest.fixture
def shell(qtbot: Any, qt_messages: list[str]) -> Iterator[Shell]:
    s = Shell(qtbot)
    yield s
    s.view.teardown()
    s.view.deleteLater()


def test_only_the_penguin_takes_input_when_nothing_is_open(shell: Shell) -> None:
    window = QRect(0, 0, shell.view.width(), shell.view.height())
    assert len(shell.rects[-1]) == 1
    assert window.contains(shell.penguin) and shell.penguin.width() < window.width() / 2
    # bottom-right corner, as placed by the layout
    assert shell.penguin.right() > window.width() * 0.8
    assert shell.penguin.bottom() > window.height() * 0.9


def test_bubble_card_and_pill_join_the_input_mask_only_while_visible(shell: Shell) -> None:
    shell.bridge.set_bubble("Hello there")
    rects = shell.settle()
    assert len(rects) == 2 and rects[1].bottom() <= shell.penguin.top() + 8

    shell.bridge.set_bubble("")
    shell.bridge.set_guide(mock_guide(), card_open=True)
    rects = shell.settle()
    assert len(rects) == 2 and rects[1].height() > 150  # the card, bubble gone
    assert shell.bridge.property("pillText") == ""

    shell.bridge.set_card_open(False)
    rects = shell.settle()
    assert shell.bridge.property("pillText") == "2/5 · Open the Material tab"
    assert len(rects) == 2 and rects[1].height() < 60  # the pill
    assert rects[1].right() <= shell.penguin.left()

    shell.bridge.set_guide(None)
    assert len(shell.settle()) == 1


def test_dodging_moves_the_input_mask_out_of_the_way(shell: Shell) -> None:
    before = QRect(shell.penguin)
    shell.bridge.set_dodged(True)
    after = shell.settle()[0]
    assert after.right() < before.left(), "the old spot must be fully click-through"
    shell.bridge.set_dodged(False)
    assert shell.settle()[0] == before


def test_click_and_drag_on_the_body_are_told_apart(shell: Shell) -> None:
    centre = shell.penguin.center()
    with shell.qtbot.waitSignal(shell.bridge.bodyClicked, timeout=1000):
        QTest.mouseClick(shell.view, Qt.MouseButton.LeftButton, pos=centre)

    clicks: list[int] = []
    drags: list[int] = []
    shell.bridge.bodyClicked.connect(lambda: clicks.append(1))
    shell.bridge.dragRequested.disconnect(shell.view.startSystemMove)
    shell.bridge.dragRequested.connect(lambda: drags.append(1))
    QTest.mousePress(shell.view, Qt.MouseButton.LeftButton, pos=centre)
    QTest.mouseMove(shell.view, centre + QPoint(20, 10))
    QTest.mouseMove(shell.view, centre + QPoint(40, 20))
    QTest.mouseRelease(shell.view, Qt.MouseButton.LeftButton, pos=centre + QPoint(40, 20))
    shell.qtbot.wait(30)
    assert drags == [1] and clicks == [], "a drag must not also count as a click"


def test_clicking_outside_the_body_reaches_nothing(shell: Shell) -> None:
    clicks: list[int] = []
    shell.bridge.bodyClicked.connect(lambda: clicks.append(1))
    QTest.mouseClick(shell.view, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    shell.qtbot.wait(30)
    assert clicks == []


def test_card_input_opens_the_ask_window_instead_of_taking_focus(shell: Shell) -> None:
    shell.bridge.set_guide(mock_guide(), card_open=True)
    shell.settle()
    field = shell.view.rootObject().findChild(object, "input")  # type: ignore[union-attr]
    assert field is not None
    centre = field.mapToItem(shell.view.rootObject(), field.width() / 2, field.height() / 2)
    with shell.qtbot.waitSignal(shell.bridge.askRequested, timeout=1000):
        QTest.mouseClick(shell.view, Qt.MouseButton.LeftButton, pos=centre.toPoint())
    assert shell.view.flags() & Qt.WindowType.WindowDoesNotAcceptFocus


def test_card_buttons_drive_the_demo_guide(shell: Shell) -> None:
    demo = DemoDriver(shell.bridge)
    demo.toggle_guide()
    shell.settle()
    root = shell.view.rootObject()
    assert root is not None

    def click(name: str) -> None:
        button = root.findChild(object, name)
        assert button is not None, name
        pos = button.mapToItem(root, button.width() / 2, button.height() / 2).toPoint()
        QTest.mouseClick(shell.view, Qt.MouseButton.LeftButton, pos=pos)
        shell.qtbot.wait(30)

    click("btnWhy")
    assert shell.bridge.property("showWhy") is True
    click("btnDone")
    guide = shell.bridge.guide_view
    assert guide is not None and guide.current == 2 and guide.steps[1].done
    assert shell.bridge.current_mood is Mood.CELEBRATE
    assert shell.bridge.property("showWhy") is False
    click("btnBack")
    guide = shell.bridge.guide_view
    assert guide is not None and guide.current == 1 and not guide.steps[1].done


def test_every_mood_renders_in_the_assembled_window(shell: Shell) -> None:
    for mood in Mood:
        shell.bridge.set_mood(mood)
        shell.qtbot.wait(20)
        assert shell.view.rootObject().findChild(object, "penguin").property("mood") == mood.value  # type: ignore[union-attr]


def test_animations_stop_when_the_window_is_not_exposed(qtbot: Any, qt_messages: list[str]) -> None:
    s = Shell(qtbot)
    s.bridge.set_view(motion_scale=1.0)
    penguin = s.view.rootObject().findChild(object, "penguin")  # type: ignore[union-attr]
    qtbot.waitUntil(lambda: penguin.property("animating") is True, timeout=2000)
    s.view.hide()
    qtbot.waitUntil(lambda: s.bridge.property("running") is False, timeout=2000)
    assert penguin.property("animating") is False
    s.view.show()
    qtbot.waitUntil(lambda: penguin.property("animating") is True, timeout=2000)
    s.view.teardown()


def test_demo_advance_is_bounded() -> None:
    guide = mock_guide()
    for _ in range(10):
        guide = advance(guide, "done")
    assert guide.current == guide.total - 1 and all(s.done for s in guide.steps)
    for _ in range(10):
        guide = advance(guide, "back")
    assert guide.current == 0 and not guide.steps[0].done
    assert advance(guide, "skip").steps[0].done is False


def test_bridge_rejects_unknown_actions_and_blank_text(qtbot: Any) -> None:
    bridge = Bridge(ThemeObject(FALLBACK_LIGHT))
    actions: list[str] = []
    texts: list[str] = []
    bridge.guideActionRequested.connect(actions.append)
    bridge.textSubmitted.connect(texts.append)
    for name in ("done", "rm -rf", "", "why"):
        bridge.guideAction(name)
    bridge.submitText("   ")
    bridge.submitText("  how do I render?  ")
    bridge.submitText("x" * 10_000)
    assert actions == ["done", "why"]
    assert texts[0] == "how do I render?" and len(texts[1]) == 4000


def test_tray_menu_has_the_full_shape_and_reflects_state(qtbot: Any) -> None:
    tray = Tray("#3daee9")
    texts = tray.action_texts()
    for expected in ("Pause", "Quiet mode", "Focus mode", "Show Waddle", "Settings…", "Quit"):
        assert expected in texts
    assert any(t.startswith("Waddle's eyes") for t in texts)
    assert not tray.eyes.isEnabled() and not tray.settings.isEnabled()

    paused: list[bool] = []
    tray.pauseToggled.connect(paused.append)
    tray.pause.trigger()
    assert paused == [True]

    icons = {s: paint_icon(s, "#3daee9").toImage() for s in TrayState}
    assert icons[TrayState.WATCHING] != icons[TrayState.PAUSED] != icons[TrayState.BRAIN_DOWN]
    assert paint_icon(TrayState.WATCHING, "#ff00ff").toImage() != icons[TrayState.WATCHING]
    assert "Demo" not in texts and "Demo" in Tray("#3daee9", demo=True).action_texts()
