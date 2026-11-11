"""Bubble, GuideCard, StepPill, Keycap and WButton: behavior, accessibility, theming."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    Qt,
    QtMsgType,
    QUrl,
    qInstallMessageHandler,
)
from PySide6.QtGui import QColor, QImage, QKeyEvent
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

import waddle_desktop
from waddle_core.guide import mock_guide
from waddle_desktop.theme.palette import FALLBACK_DARK, FALLBACK_LIGHT
from waddle_desktop.ui.theme_object import ThemeObject

Loader = Callable[..., QObject]
Show = Callable[[QObject], QQuickWindow]

LONG = "Lower the roughness for sharper reflections, then raise metallic a little. " * 8
BACKDROP = QColor("#ff00ff")


@pytest.fixture
def theme(qapp: Any) -> ThemeObject:
    return ThemeObject(FALLBACK_LIGHT)


@pytest.fixture
def load_qml(qml_engine: QQmlEngine) -> Iterator[Loader]:
    """Like conftest's ``load_qml``, but keeps each QQmlComponent alive for the whole test.

    PySide invalidates the wrapper of a created object as soon as its component is collected,
    so the shared fixture's objects are unusable once it returns.
    """
    qml_dir = Path(waddle_desktop.__file__).parent / "qml"
    alive: list[tuple[QQmlComponent, QObject]] = []

    def _load(name: str, **props: Any) -> QObject:
        component = QQmlComponent(qml_engine, QUrl.fromLocalFile(str(qml_dir / name)))
        assert component.isReady(), [e.toString() for e in component.errors()]
        obj = component.createWithInitialProperties(props)
        assert obj is not None, [e.toString() for e in component.errors()]
        alive.append((component, obj))
        return obj

    yield _load
    for _component, obj in alive:
        obj.deleteLater()


@pytest.fixture(autouse=True)
def qml_warnings() -> Iterator[list[str]]:
    """Every warning Qt prints during a test; each test ends by requiring there were none."""
    seen: list[str] = []

    def handler(kind: QtMsgType, _context: object, message: str) -> None:
        if kind != QtMsgType.QtDebugMsg and kind != QtMsgType.QtInfoMsg:
            seen.append(message)

    qInstallMessageHandler(handler)
    yield seen
    qInstallMessageHandler(None)
    assert seen == []


@pytest.fixture
def show(qtbot: Any) -> Iterator[Show]:
    """Put an item in a real (offscreen) window so it renders and takes input."""
    windows: list[QQuickWindow] = []

    def _show(obj: QObject) -> QQuickWindow:
        assert isinstance(obj, QQuickItem)
        window = QQuickWindow()
        window.setColor(BACKDROP)
        window.resize(400, 400)
        obj.setParentItem(window.contentItem())
        obj.setPosition(QPointF(10, 10))
        window.show()
        qtbot.waitExposed(window)
        qtbot.wait(30)  # layouts settle on the first polish
        window.resize(max(60, int(obj.width()) + 20), max(40, int(obj.height()) + 20))
        qtbot.wait(10)
        windows.append(window)
        return window

    yield _show
    for window in windows:
        window.close()
        window.deleteLater()


def descendants(item: QQuickItem, name: str) -> list[QQuickItem]:
    """Visual children by objectName (Repeater delegates are not QObject children)."""
    found = [item] if item.objectName() == name else []
    for child in item.childItems():
        found += descendants(child, name)
    return found


def one(root: QObject, name: str) -> QQuickItem:
    assert isinstance(root, QQuickItem)
    found = descendants(root, name)
    assert len(found) == 1, f"{name}: {len(found)} matches"
    return found[0]


def click(window: QQuickWindow, item: QQuickItem) -> None:
    center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)


def record(obj: QObject, signal: str) -> list[Any]:
    """Connect to a QML signal; the list receives its first argument (or None) per emission."""
    hits: list[Any] = []
    getattr(obj, signal).connect(lambda *args: hits.append(args[0] if args else None))
    return hits


def grab(window: QQuickWindow) -> QImage:
    image = window.grabWindow()
    assert not image.isNull()
    return image


def colors_in(image: QImage) -> set[int]:
    return {
        image.pixel(x, y) for x in range(0, image.width(), 3) for y in range(0, image.height(), 3)
    }


# --- every component -------------------------------------------------------------------------

COMPONENTS: dict[str, dict[str, Any]] = {
    "Bubble.qml": {"text": LONG},
    "GuideCard.qml": {"guide": mock_guide().to_qml(), "showWhy": True},
    "StepPill.qml": {"text": "2/5 · Open the Material tab"},
    "Keycap.qml": {"text": "Shift"},
    "WButton.qml": {"text": "I did it", "primary": True},
}


@pytest.mark.parametrize("name", COMPONENTS)
@pytest.mark.parametrize("palette", [FALLBACK_LIGHT, FALLBACK_DARK], ids=["light", "dark"])
@pytest.mark.parametrize("motion", [1.0, 0.0])
def test_component_renders_in_both_themes(
    load_qml: Loader, show: Show, name: str, palette: Any, motion: float
) -> None:
    item = load_qml(name, theme=ThemeObject(palette), motionScale=motion, **COMPONENTS[name])
    assert isinstance(item, QQuickItem)
    assert item.implicitWidth() > 0 and item.implicitHeight() > 0
    image = grab(show(item))
    assert len(colors_in(image) - {BACKDROP.rgb()}) >= 3  # drew a fill, a border and some text


@pytest.mark.parametrize("name", COMPONENTS)
def test_component_survives_missing_theme(load_qml: Loader, name: str) -> None:
    item = load_qml(name, **COMPONENTS[name])
    assert isinstance(item, QQuickItem) and item.implicitHeight() > 0


@pytest.mark.parametrize("name", ["StepPill.qml", "Keycap.qml", "WButton.qml"])
def test_accessible_name_is_the_text(load_qml: Loader, theme: ThemeObject, name: str) -> None:
    item = load_qml(name, theme=theme, text="Open the Material tab")
    assert item.property("accessibleText") == "Open the Material tab"


# --- Bubble ----------------------------------------------------------------------------------


def test_bubble_long_text_truncates_and_expands(load_qml: Loader, theme: ThemeObject) -> None:
    bubble = load_qml("Bubble.qml", theme=theme, text=LONG)
    assert isinstance(bubble, QQuickItem)
    assert bubble.property("truncated") is True
    assert bubble.implicitWidth() <= 40 * 8 + 1  # 320 design px at the 10 pt fallback
    collapsed = bubble.implicitHeight()
    bubble.setProperty("expanded", True)
    expanded = bubble.implicitHeight()
    assert collapsed < expanded
    # Expanded is capped (~12 lines), so three times the text does not grow the bubble.
    bubble.setProperty("text", LONG * 3)
    assert bubble.implicitHeight() == pytest.approx(expanded, abs=1.0)
    # More lines allowed while collapsed means a taller collapsed bubble.
    bubble.setProperty("expanded", False)
    bubble.setProperty("maxLines", 6)
    assert collapsed < bubble.implicitHeight() < expanded


def test_bubble_short_text_is_not_truncated(load_qml: Loader, theme: ThemeObject) -> None:
    bubble = load_qml("Bubble.qml", theme=theme, text="Hi! I'm Waddle.")
    assert isinstance(bubble, QQuickItem)
    assert bubble.property("truncated") is False
    assert one(bubble, "more").isVisible() is False
    assert bubble.implicitWidth() < 200


def test_bubble_accessible_name_is_full_text_even_when_collapsed(
    load_qml: Loader, theme: ThemeObject
) -> None:
    bubble = load_qml("Bubble.qml", theme=theme, text=LONG)
    assert bubble.property("truncated") is True
    assert bubble.property("accessibleText") == LONG


def test_bubble_streaming_keeps_width_and_emits_no_warnings(
    load_qml: Loader, show: Show, theme: ThemeObject, qtbot: Any, qml_warnings: list[str]
) -> None:
    bubble = load_qml("Bubble.qml", theme=theme, text="Materials decide how a surface reacts")
    assert isinstance(bubble, QQuickItem)
    show(bubble)
    text, widths, heights = str(bubble.property("text")), set(), []
    for i in range(50):
        text += f" token{i}"
        bubble.setProperty("text", text)
        qtbot.wait(1)
        widths.add(bubble.implicitWidth())
        heights.append(bubble.implicitHeight())
    assert bubble.property("accessibleText") == text
    assert len(widths) == 1  # already at max width: growing text never changes it
    assert heights == sorted(heights)  # height only ever grows, then stops at maxLines
    assert bubble.property("truncated") is True
    assert qml_warnings == []


def test_bubble_more_and_close_are_real_clicks(
    load_qml: Loader, show: Show, theme: ThemeObject
) -> None:
    bubble = load_qml("Bubble.qml", theme=theme, text=LONG)
    window = show(bubble)
    more, dismissed = record(bubble, "moreClicked"), record(bubble, "dismissed")
    click(window, one(bubble, "more"))
    assert (len(more), len(dismissed)) == (1, 0)
    click(window, one(bubble, "close"))
    assert (len(more), len(dismissed)) == (1, 1)
    assert one(bubble, "close").property("accessibleText") == "Dismiss"


def test_bubble_tail_follows_tail_right(load_qml: Loader, show: Show, theme: ThemeObject) -> None:
    bubble = load_qml("Bubble.qml", theme=theme, text=LONG)
    assert isinstance(bubble, QQuickItem)
    window = show(bubble)
    view_bg = QColor(theme.palette.view_bg).rgb()

    def tail_columns() -> list[int]:
        image = grab(window)
        y = int(10 + bubble.height() - 8 - 4)  # inside the tail, below the panel's bottom edge
        return [x for x in range(image.width()) if image.pixel(x, y) == view_bg]

    right = tail_columns()
    bubble.setProperty("tailRight", False)
    left = tail_columns()
    middle = 10 + bubble.width() / 2
    assert right and left
    assert min(right) > middle > max(left)


# --- GuideCard -------------------------------------------------------------------------------


def test_card_shows_progress_and_earlier_steps(load_qml: Loader, theme: ThemeObject) -> None:
    card = load_qml("GuideCard.qml", theme=theme, guide=mock_guide().to_qml())
    assert isinstance(card, QQuickItem)
    assert one(card, "goal").property("text") == "Render a glossy cube with Cycles"
    assert one(card, "progress").property("text") == "Step 2 of 5"
    assert one(card, "stepTitle").property("text") == "Open the Material tab"
    assert len(descendants(card, "doneStep")) == 1
    assert descendants(card, "skippedStep") == []
    assert descendants(card, "keycap") == []  # the current step has no shortcut
    assert one(card, "btnBack").isEnabled() is True
    assert card.property("inputField") is one(card, "input")
    assert one(card, "input").property("placeholderText") == "Ask Waddle…"
    spoken = str(card.property("accessibleText"))
    assert "Step 2 of 5" in spoken and "click the red sphere icon." in spoken


def test_card_renders_shortcuts_as_keycaps(
    load_qml: Loader, show: Show, theme: ThemeObject
) -> None:
    guide = mock_guide().model_copy(update={"current": 0}).to_qml()
    card = load_qml("GuideCard.qml", theme=theme, guide=guide)
    assert isinstance(card, QQuickItem)
    show(card)
    caps = descendants(card, "keycap")
    assert [c.property("text") for c in caps] == ["Shift", "A"]
    words = [w.property("text") for w in descendants(card, "word")]
    assert words == ["Press", "+", "and", "choose", "Mesh", "→", "Cube."]
    flow = one(card, "instruction")
    assert flow.height() >= caps[0].height() > 0
    assert all(c.x() >= 0 and c.x() + c.width() <= flow.width() for c in caps)  # wrapped inside
    assert "Press Shift+A and choose" in str(card.property("accessibleText"))
    assert descendants(card, "doneStep") == []
    assert one(card, "btnBack").isEnabled() is False  # first step


def test_card_why_text_follows_show_why(
    load_qml: Loader, show: Show, theme: ThemeObject, qtbot: Any
) -> None:
    card = load_qml("GuideCard.qml", theme=theme, guide=mock_guide().to_qml())
    assert isinstance(card, QQuickItem)
    show(card)
    why = one(card, "why")
    assert why.property("text") == "Materials decide how a surface reacts to light."
    assert why.isVisible() is False
    before = card.implicitHeight()
    card.setProperty("showWhy", True)
    assert why.isVisible() is True
    qtbot.waitUntil(lambda: card.implicitHeight() > before, timeout=2000)


@pytest.mark.parametrize(
    ("button", "name"),
    [
        ("btnBack", "back"),
        ("btnSkip", "skip"),
        ("btnDone", "done"),
        ("btnShow", "show"),
        ("btnWhy", "why"),
    ],
)
def test_card_button_click_emits_action(
    load_qml: Loader, show: Show, theme: ThemeObject, button: str, name: str
) -> None:
    card = load_qml("GuideCard.qml", theme=theme, guide=mock_guide().to_qml())
    window = show(card)
    actions = record(card, "action")
    item = one(card, button)
    assert item.property("accessibleText")
    assert item.property("activeFocusOnTab") is True
    click(window, item)
    assert actions == [name]


def test_card_disabled_back_ignores_clicks(
    load_qml: Loader, show: Show, theme: ThemeObject
) -> None:
    guide = mock_guide().model_copy(update={"current": 0}).to_qml()
    card = load_qml("GuideCard.qml", theme=theme, guide=guide)
    window = show(card)
    actions = record(card, "action")
    click(window, one(card, "btnBack"))
    assert actions == []


def test_card_buttons_work_from_the_keyboard(
    load_qml: Loader, show: Show, theme: ThemeObject, qtbot: Any
) -> None:
    card = load_qml("GuideCard.qml", theme=theme, guide=mock_guide().to_qml())
    window = show(card)
    actions = record(card, "action")
    window.requestActivate()
    one(card, "btnShow").forceActiveFocus()
    qtbot.waitUntil(lambda: one(card, "btnShow").hasActiveFocus(), timeout=2000)
    QTest.keyClick(window, Qt.Key.Key_Space)
    QTest.keyClick(window, Qt.Key.Key_Tab)
    assert one(card, "btnWhy").hasActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert actions == ["show", "why"]


def test_card_held_key_fires_a_button_only_once(
    load_qml: Loader, show: Show, theme: ThemeObject, qtbot: Any
) -> None:
    card = load_qml("GuideCard.qml", theme=theme, guide=mock_guide().to_qml())
    window = show(card)
    actions = record(card, "action")
    window.requestActivate()
    one(card, "btnDone").forceActiveFocus()
    qtbot.waitUntil(lambda: one(card, "btnDone").hasActiveFocus(), timeout=2000)
    none = Qt.KeyboardModifier.NoModifier
    for repeat in (False, True, True, True):  # one press, then the keyboard's auto-repeat
        press = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, none, "\r", repeat)
        QCoreApplication.sendEvent(window, press)
    assert actions == ["done"]  # holding Enter must not skip through several steps


@pytest.mark.parametrize(
    "guide",
    [
        {"steps": [None, None], "current": 1},
        {"steps": "abc"},
        {"steps": {"a": 1}},
        5,
    ],
    ids=str,
)
def test_card_treats_malformed_guides_as_no_guide(
    load_qml: Loader, theme: ThemeObject, guide: Any
) -> None:
    card = load_qml("GuideCard.qml", theme=theme, guide=guide)
    assert isinstance(card, QQuickItem)
    assert not card.isVisible() and card.implicitHeight() == 0


def test_card_skips_malformed_segments(load_qml: Loader, theme: ThemeObject) -> None:
    step = {"title": "Render", "segments": [None, {"text": "Press"}, {"keys": "F12"}, 7]}
    card = load_qml("GuideCard.qml", theme=theme, guide={"goal": "g", "steps": [step]})
    assert isinstance(card, QQuickItem)
    assert card.isVisible() and card.implicitHeight() > 0
    assert [w.property("text") for w in descendants(card, "word")] == ["Press"]
    assert descendants(card, "keycap") == []  # keys must be a list, not a string


def test_card_input_submits_on_enter_and_clears(
    load_qml: Loader, show: Show, theme: ThemeObject, qtbot: Any
) -> None:
    card = load_qml("GuideCard.qml", theme=theme, guide=mock_guide().to_qml())
    window = show(card)
    submitted = record(card, "submitted")
    field = one(card, "input")
    window.requestActivate()
    click(window, field)
    qtbot.waitUntil(field.hasActiveFocus, timeout=2000)
    QTest.keyClick(window, Qt.Key.Key_Return)  # empty: nothing to send
    assert submitted == []
    for char in "where is roughness":
        QTest.keyClick(window, char)  # type: ignore[call-overload]
    assert field.property("text") == "where is roughness"
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert submitted == ["where is roughness"]
    assert field.property("text") == ""


@pytest.mark.parametrize("guide", [None, {}, {"goal": "x", "steps": []}], ids=str)
def test_card_without_a_guide_renders_nothing(
    load_qml: Loader, show: Show, theme: ThemeObject, guide: Any
) -> None:
    card = load_qml("GuideCard.qml", theme=theme, guide=guide)
    assert isinstance(card, QQuickItem)
    assert (card.implicitWidth(), card.implicitHeight()) == (0, 0)
    assert colors_in(grab(show(card))) == {BACKDROP.rgb()}
    # ...and it comes to life (and goes away again) when the guide changes.
    card.setProperty("guide", mock_guide().to_qml())
    assert card.implicitHeight() > 0 and card.isVisible()
    card.setProperty("guide", None)
    assert card.implicitHeight() == 0 and not card.isVisible()


def test_card_clamps_out_of_range_current(load_qml: Loader, theme: ThemeObject) -> None:
    guide = mock_guide().to_qml()
    guide["current"] = 99
    card = load_qml("GuideCard.qml", theme=theme, guide=guide)
    assert isinstance(card, QQuickItem)
    assert card.property("currentIndex") == 4
    assert one(card, "stepTitle").property("text") == "Render"
    assert [c.property("text") for c in descendants(card, "keycap")] == ["F12"]
    assert len(descendants(card, "doneStep")) == 1  # only "Add a cube" is marked done
    assert len(descendants(card, "skippedStep")) == 3
    guide["current"] = -3
    card.setProperty("guide", guide)
    assert card.property("currentIndex") == 0
    assert one(card, "stepTitle").property("text") == "Add a cube"


def test_card_background_follows_theme(load_qml: Loader, show: Show) -> None:
    theme = ThemeObject(FALLBACK_LIGHT)
    card = load_qml("GuideCard.qml", theme=theme, guide=mock_guide().to_qml())
    window = show(card)
    panel = one(card, "panel")
    spot = panel.mapToScene(QPointF(panel.width() - 6, panel.height() / 2)).toPoint()  # padding

    def background() -> int:
        return grab(window).pixel(QPoint(spot.x(), spot.y()))

    assert background() == QColor(FALLBACK_LIGHT.window_bg).rgb()
    theme.apply(FALLBACK_DARK)
    assert background() == QColor(FALLBACK_DARK.window_bg).rgb()


# --- StepPill --------------------------------------------------------------------------------


def test_pill_elides_long_text(load_qml: Loader, theme: ThemeObject) -> None:
    short = load_qml("StepPill.qml", theme=theme, text="2/5 · Render")
    long = load_qml("StepPill.qml", theme=theme, text="2/5 · " + "Open the Material tab " * 6)
    assert isinstance(short, QQuickItem) and isinstance(long, QQuickItem)
    assert short.property("truncated") is False
    assert long.property("truncated") is True
    assert short.implicitWidth() < long.implicitWidth() == long.property("maxWidth")
    assert long.implicitHeight() == short.implicitHeight()  # still a single line
    assert str(long.property("accessibleText")).endswith("Open the Material tab ")


def test_pill_click_emits_clicked(load_qml: Loader, show: Show, theme: ThemeObject) -> None:
    pill = load_qml("StepPill.qml", theme=theme, text="2/5 · Open the Material tab")
    assert isinstance(pill, QQuickItem)
    window = show(pill)
    spy = record(pill, "clicked")
    click(window, pill)
    assert len(spy) == 1


# --- WButton ---------------------------------------------------------------------------------


def test_button_click_and_disabled_look(load_qml: Loader, show: Show, theme: ThemeObject) -> None:
    button = load_qml("WButton.qml", theme=theme, text="Skip")
    assert isinstance(button, QQuickItem)
    window = show(button)
    spy = record(button, "clicked")
    click(window, button)
    assert len(spy) == 1
    enabled_look = grab(window)
    button.setEnabled(False)
    click(window, button)
    assert len(spy) == 1
    assert grab(window) != enabled_look


def test_primary_button_uses_the_accent(load_qml: Loader, show: Show, theme: ThemeObject) -> None:
    button = load_qml("WButton.qml", theme=theme, text="I did it", primary=True)
    assert QColor(theme.palette.accent).rgb() in colors_in(grab(show(button)))
