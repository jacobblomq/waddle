"""The minimal penguin icon: same contract as the detailed character."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from PySide6.QtCore import QObject, QtMsgType, QUrl, qInstallMessageHandler
from PySide6.QtGui import QColor, QImage
from PySide6.QtQuick import QQuickView

from waddle_core.mood import Mood
from waddle_desktop.theme.palette import FALLBACK_LIGHT
from waddle_desktop.ui.app import QML_DIR, PenguinView
from waddle_desktop.ui.bridge import Bridge
from waddle_desktop.ui.theme_object import ThemeObject

MOODS = [m.value for m in Mood]


@pytest.fixture(autouse=True)
def no_qt_warnings() -> Iterator[None]:
    seen: list[str] = []

    def handler(kind: QtMsgType, _ctx: Any, message: str) -> None:
        if kind != QtMsgType.QtDebugMsg and "window masks" not in message:
            seen.append(message)

    previous = qInstallMessageHandler(handler)
    yield
    qInstallMessageHandler(previous)
    assert seen == []


def render(qtbot: Any, **props: Any) -> QImage:
    view = QQuickView()
    view.setColor(QColor("#808080"))
    view.setInitialProperties({"width": 144, "height": 160, "motionScale": 0, **props})
    view.setSource(QUrl.fromLocalFile(str(QML_DIR / "PenguinMini.qml")))
    assert not view.errors()
    view.show()
    qtbot.waitExposed(view)
    qtbot.wait(40)
    image = view.grabWindow()
    view.setSource(QUrl())
    view.deleteLater()
    return image


def test_every_mood_has_its_own_name_and_picture(
    load_qml: Callable[..., QObject], qtbot: Any
) -> None:
    item = load_qml("PenguinMini.qml")
    names = set()
    for mood in MOODS:
        item.setProperty("mood", mood)
        names.add(item.property("accessibleText"))
    assert len(names) == len(MOODS)

    pictures = {mood: render(qtbot, mood=mood) for mood in MOODS}
    steady = [m for m in MOODS if m not in {"celebrate", "alert", "speaking", "watching"}]
    for i, a in enumerate(steady):
        for b in steady[i + 1 :]:
            assert pictures[a] != pictures[b], f"{a} and {b} look identical"
    assert pictures["alert"] != pictures["idle"]
    assert render(qtbot, mood="speaking", mouthOpen=1.0) != render(qtbot, mood="speaking")
    assert render(qtbot, mood="watching", lookX=1.0) != render(qtbot, mood="watching")


def test_unknown_mood_falls_back_to_idle(load_qml: Callable[..., QObject]) -> None:
    item = load_qml("PenguinMini.qml", mood="definitely-not-a-mood")
    assert item.property("accessibleText") == "Waddle is idle"


@pytest.mark.parametrize("gate", [{"running": False}, {"motionScale": 0}])
def test_nothing_animates_when_paused_or_reduced_motion(
    load_qml: Callable[..., QObject], gate: dict[str, Any]
) -> None:
    item = load_qml("PenguinMini.qml", **gate)
    for mood in MOODS:
        item.setProperty("mood", mood)
        assert item.property("animating") is False, mood


def test_idle_blinks_sparsely_and_sleep_breathes(load_qml: Callable[..., QObject]) -> None:
    item = load_qml("PenguinMini.qml")
    assert item.property("animating") is True  # the blink timer is armed
    timer = [t for t in item.findChildren(QObject) if t.metaObject().className() == "QQmlTimer"]
    assert timer and all(t.property("interval") >= 2500 for t in timer)
    item.setProperty("mood", "sleeping")
    assert item.property("animating") is True
    item.setProperty("mood", "eyes-closed")
    assert item.property("animating") is False, "privacy pose is completely still"


@pytest.mark.parametrize("mood", ["celebrate", "alert"])
@pytest.mark.parametrize("motion", [0.05, 0.0])
def test_one_shot_moods_finish(
    load_qml: Callable[..., QObject], qtbot: Any, mood: str, motion: float
) -> None:
    item = load_qml("PenguinMini.qml", motionScale=motion)
    with qtbot.waitSignal(item.transientFinished, timeout=2000):
        item.setProperty("mood", mood)


def test_leaving_a_one_shot_mood_early_emits_nothing(
    load_qml: Callable[..., QObject], qtbot: Any
) -> None:
    item = load_qml("PenguinMini.qml", motionScale=0.0)
    fired: list[int] = []
    item.transientFinished.connect(lambda: fired.append(1))
    item.setProperty("mood", "celebrate")
    item.setProperty("mood", "idle")
    qtbot.wait(60)
    assert fired == []


def test_figure_stays_inside_its_body_rect_and_accent_shows_when_listening(qtbot: Any) -> None:
    image = render(qtbot, mood="idle")
    background = image.pixelColor(1, 1)
    xs = [
        x
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y) != background
    ]
    ys = [
        y
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y) != background
    ]
    # bodyRect at this size is (18, 22, 108, 132)
    assert min(xs) >= 18 and max(xs) <= 126 and min(ys) >= 22 and max(ys) <= 154

    listening = render(qtbot, mood="listening", accentColor="#ff00ff", level=1.0)
    assert listening.pixelColor(72, 16) == QColor("#ff00ff")
    assert image.pixelColor(72, 16) == background


@pytest.mark.parametrize(("style", "width"), [("minimal", 72), ("detailed", 160)])
def test_the_window_uses_the_configured_style(qtbot: Any, style: str, width: int) -> None:
    bridge = Bridge(ThemeObject(FALLBACK_LIGHT))
    bridge.set_view(style=style, motion_scale=0.0)
    rects: list[list[Any]] = []
    bridge.inputRectsChanged.connect(rects.append)
    view = PenguinView(bridge)
    view.setSource(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
    view.show()
    qtbot.waitUntil(lambda: bool(rects and rects[-1]), timeout=2000)
    penguin = view.rootObject().findChild(QObject, "penguin")  # type: ignore[union-attr]
    assert penguin.property("width") == width
    assert 0 < rects[-1][0].width() <= width
    view.teardown()
