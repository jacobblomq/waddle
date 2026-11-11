"""The penguin character: API contract, motion gating, and an offscreen render check."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject, QPoint, QRectF, QtMsgType, QUrl, qInstallMessageHandler
from PySide6.QtGui import QColor, QImage
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlProperty
from PySide6.QtQuick import QQuickItem, QQuickView

import waddle_desktop

PENGUIN = Path(waddle_desktop.__file__).parent / "qml" / "Penguin.qml"
MOODS = [
    "idle",
    "listening",
    "watching",
    "thinking",
    "speaking",
    "celebrate",
    "alert",
    "confused",
    "eyes-closed",
    "sleeping",
    "napping-brain",
]
Loader = Callable[..., QObject]


@pytest.fixture
def load_qml(qml_engine: QQmlEngine) -> Iterator[Loader]:
    """conftest's ``load_qml`` plus one fix: the QQmlComponent stays alive for the whole test.

    PySide deletes the created object when its component is garbage collected, which with the
    shared fixture happens as soon as the loader returns.
    """
    alive: list[tuple[QQmlComponent, QObject]] = []

    def _load(name: str, **props: Any) -> QObject:
        component = QQmlComponent(qml_engine, QUrl.fromLocalFile(str(PENGUIN.parent / name)))
        assert component.isReady(), [e.toString() for e in component.errors()]
        obj = component.createWithInitialProperties(props)
        assert obj is not None, [e.toString() for e in component.errors()]
        alive.append((component, obj))
        return obj

    yield _load
    for _component, obj in alive:
        obj.deleteLater()


@pytest.fixture
def qt_messages() -> Iterator[list[str]]:
    """Every Qt/QML message emitted during the test (warnings included)."""
    seen: list[str] = []

    def handler(_kind: QtMsgType, _ctx: Any, text: str) -> None:
        seen.append(text)

    previous = qInstallMessageHandler(handler)
    yield seen
    qInstallMessageHandler(previous)


def accessible_name(obj: QObject, engine: QQmlEngine) -> str:
    prop = QQmlProperty(obj, "Accessible.name", engine.contextForObject(obj))
    assert prop.isValid()
    return str(prop.read())


def test_loads_with_documented_defaults(load_qml: Loader, qt_messages: list[str]) -> None:
    penguin = load_qml("Penguin.qml")
    assert isinstance(penguin, QQuickItem)
    assert (penguin.implicitWidth(), penguin.implicitHeight()) == (160, 190)
    assert penguin.property("mood") == "idle"
    assert penguin.property("accentColor") == QColor("#3daee9")
    assert penguin.property("motionScale") == 1.0
    assert penguin.property("running") is True
    assert penguin.property("animating") is True  # idle keeps its sparse blink timer armed
    assert qt_messages == []


def test_every_mood_is_clean_and_named(
    load_qml: Loader, qml_engine: QQmlEngine, qtbot: Any, qt_messages: list[str]
) -> None:
    penguin = load_qml("Penguin.qml")
    names = {}
    for mood in MOODS:
        penguin.setProperty("mood", mood)
        qtbot.wait(20)
        assert penguin.property("state") == mood
        names[mood] = accessible_name(penguin, qml_engine)
        assert names[mood].startswith("Waddle ")
    assert len(set(names.values())) == len(MOODS), names
    assert names["thinking"] == "Waddle is thinking"
    assert qt_messages == []


def test_unknown_mood_falls_back_to_idle(load_qml: Loader, qt_messages: list[str]) -> None:
    penguin = load_qml("Penguin.qml", mood="moonwalking")
    assert penguin.property("state") == "idle"
    assert qt_messages == []


@pytest.mark.parametrize("gate", [{"running": False}, {"motionScale": 0.0}])
def test_nothing_animates_when_paused_or_reduced_motion(
    load_qml: Loader, qtbot: Any, gate: dict[str, Any]
) -> None:
    penguin = load_qml("Penguin.qml", **gate)
    for mood in MOODS:
        penguin.setProperty("mood", mood)
        assert penguin.property("animating") is False, mood
        qtbot.wait(10)
        assert penguin.property("animating") is False, mood


def test_pausing_stops_running_animations_and_resuming_restarts(load_qml: Loader) -> None:
    penguin = load_qml("Penguin.qml", mood="sleeping")
    assert penguin.property("animating") is True
    penguin.setProperty("running", False)
    assert penguin.property("animating") is False
    penguin.setProperty("running", True)
    assert penguin.property("animating") is True
    penguin.setProperty("motionScale", 0.5)
    assert penguin.property("animating") is True
    penguin.setProperty("motionScale", 0.0)
    assert penguin.property("animating") is False


@pytest.mark.parametrize("gate", [{"running": False}, {"motionScale": 0.0}])
def test_pausing_mid_transition_stops_it_and_snaps_the_pose(
    grab: Callable[..., QImage], gate: dict[str, Any]
) -> None:
    root = grab.root()  # type: ignore[attr-defined]
    still = grab(mood="confused").copy()  # the grab fixture starts with motionScale 0
    grab(mood="idle", motionScale=1.0, running=True)
    root.setProperty("mood", "confused")  # starts the 240 ms pose transition
    assert root.property("animating") is True
    for name, value in gate.items():
        root.setProperty(name, value)
    assert root.property("animating") is False
    assert grab() == still  # not frozen half-way: the static pose is the final one


def test_blink_timer_is_sparse(load_qml: Loader) -> None:
    penguin = load_qml("Penguin.qml")
    timers = [
        child
        for child in penguin.findChildren(QObject)
        if child.metaObject().className().startswith("QQmlTimer")
    ]
    assert timers, "expected the blink timer"
    assert all(timer.property("interval") >= 2500 for timer in timers)


def test_calm_moods_without_open_eyes_are_fully_idle(load_qml: Loader, qtbot: Any) -> None:
    penguin = load_qml("Penguin.qml", motionScale=0.05)
    penguin.setProperty("mood", "eyes-closed")
    qtbot.waitUntil(lambda: penguin.property("animating") is False, timeout=1000)


@pytest.mark.parametrize("mood", ["celebrate", "alert"])
def test_one_shot_moods_emit_transient_finished(load_qml: Loader, qtbot: Any, mood: str) -> None:
    penguin = load_qml("Penguin.qml", motionScale=0.05)
    with qtbot.waitSignal(penguin.transientFinished, timeout=2000):
        penguin.setProperty("mood", mood)
        assert penguin.property("animating") is True
    # Finished means finished: in a mood with no blink timer, nothing is left running.
    penguin.setProperty("mood", "eyes-closed")
    qtbot.waitUntil(lambda: penguin.property("animating") is False, timeout=1000)


def test_steady_moods_do_not_emit_transient_finished(load_qml: Loader, qtbot: Any) -> None:
    penguin = load_qml("Penguin.qml", motionScale=0.05)
    with qtbot.assertNotEmitted(penguin.transientFinished, wait=150):
        penguin.setProperty("mood", "thinking")


@pytest.mark.parametrize("mood", ["celebrate", "alert"])
@pytest.mark.parametrize("gate", [{"running": False}, {"motionScale": 0.0}])
def test_one_shot_moods_still_finish_without_motion(
    load_qml: Loader, qtbot: Any, gate: dict[str, Any], mood: str
) -> None:
    penguin = load_qml("Penguin.qml", **gate)
    with qtbot.waitSignal(penguin.transientFinished, timeout=1000):
        penguin.setProperty("mood", mood)
    assert penguin.property("animating") is False


def test_no_stale_transient_finished_after_leaving_the_mood(load_qml: Loader, qtbot: Any) -> None:
    penguin = load_qml("Penguin.qml", running=False)
    with qtbot.assertNotEmitted(penguin.transientFinished, wait=100):
        penguin.setProperty("mood", "celebrate")
        penguin.setProperty("mood", "idle")


def test_leaving_a_one_shot_mood_early_does_not_emit(load_qml: Loader, qtbot: Any) -> None:
    penguin = load_qml("Penguin.qml", mood="celebrate")
    with qtbot.assertNotEmitted(penguin.transientFinished, wait=1500):
        qtbot.wait(150)
        penguin.setProperty("mood", "idle")


def test_body_rect_is_inside_the_item_and_scales(load_qml: Loader) -> None:
    penguin = load_qml("Penguin.qml")
    assert isinstance(penguin, QQuickItem)
    for width, height in [(160.0, 190.0), (320.0, 380.0), (80.0, 190.0)]:
        penguin.setWidth(width)
        penguin.setHeight(height)
        body = penguin.property("bodyRect")
        assert isinstance(body, QRectF)
        assert body.width() > width * 0.5 and body.height() > 0
        assert QRectF(0, 0, width, height).contains(body)
        assert body.contains(penguin.property("scarfProbe"))


@pytest.fixture
def grab(qapp: Any, qtbot: Any) -> Iterator[Callable[..., QImage]]:
    """Render the penguin offscreen with the given properties; nothing touches the disk."""
    view = QQuickView()
    view.setColor(QColor("#eff0f1"))
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.setInitialProperties({"motionScale": 0.0})
    view.setSource(QUrl.fromLocalFile(str(PENGUIN)))
    assert view.status() == QQuickView.Status.Ready, [e.toString() for e in view.errors()]
    view.resize(160, 190)
    view.show()
    qtbot.waitExposed(view)

    def _grab(**props: Any) -> QImage:
        root = view.rootObject()
        for name, value in props.items():
            root.setProperty(name, value)
        qtbot.wait(30)
        return view.grabWindow()

    _grab.root = view.rootObject  # type: ignore[attr-defined]
    yield _grab
    view.close()
    view.deleteLater()


def test_render_is_not_blank(grab: Callable[..., QImage]) -> None:
    image = grab()
    assert (image.width(), image.height()) == (160, 190)
    colors = {image.pixelColor(x, y).name() for x in range(0, 160, 4) for y in range(0, 190, 4)}
    assert len(colors) > 5, colors
    # The corner is background, the middle of the head is not.
    assert image.pixelColor(2, 2) == QColor("#eff0f1")
    assert image.pixelColor(80, 35) != QColor("#eff0f1")


def test_resting_figure_lies_inside_body_rect(grab: Callable[..., QImage]) -> None:
    image = grab(mood="idle")
    body = grab.root().property("bodyRect")  # type: ignore[attr-defined]
    background = QColor("#eff0f1")
    # Rows from the soles down hold only the ground shadow, which is not part of the footprint.
    outside = [
        (x, y)
        for y in range(184)
        for x in range(160)
        if image.pixelColor(x, y) != background and not body.contains(x + 0.5, y + 0.5)
    ]
    assert outside == []


def test_scarf_takes_the_accent_color(grab: Callable[..., QImage]) -> None:
    probe = grab.root().property("scarfProbe").toPoint()  # type: ignore[attr-defined]
    assert isinstance(probe, QPoint)
    assert grab().pixelColor(probe) == QColor("#3daee9")
    assert grab(accentColor=QColor("#ff00ff")).pixelColor(probe) == QColor("#ff00ff")


def test_static_poses_differ_between_moods(grab: Callable[..., QImage]) -> None:
    idle = grab(mood="idle")
    sleeping = grab(mood="sleeping")
    napping = grab(mood="napping-brain")
    assert idle != sleeping
    assert sleeping != napping
    assert grab(mood="idle") == idle  # static: same mood renders the same picture


def test_reactive_inputs_change_the_picture(grab: Callable[..., QImage]) -> None:
    assert grab(mood="speaking", mouthOpen=0.0) != grab(mouthOpen=1.0)
    assert grab(mood="watching", lookX=-1.0) != grab(lookX=1.0)
    assert grab(mood="listening", level=0.0) != grab(level=1.0)
    assert grab(mood="idle", dark=False) != grab(dark=True)
