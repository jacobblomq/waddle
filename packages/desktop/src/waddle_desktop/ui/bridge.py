"""The single object QML talks to. Lives on the UI thread; holds view state only."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Property, QObject, QRect, QRectF, Signal, Slot

from waddle_core.guide import GuideView
from waddle_core.mood import Mood
from waddle_desktop.ui.theme_object import ThemeObject


class Bridge(QObject):
    moodChanged = Signal()
    viewChanged = Signal()
    bubbleChanged = Signal()
    guideChanged = Signal()
    dodgedChanged = Signal()
    voiceChanged = Signal()

    # QML -> Python
    inputRectsChanged = Signal(list)
    dragRequested = Signal()
    bodyHoverChanged = Signal(bool)
    bodyPressed = Signal()
    bodyClicked = Signal()
    guideActionRequested = Signal(str)
    textSubmitted = Signal(str)
    askRequested = Signal()
    transientFinished = Signal()

    def __init__(self, theme: ThemeObject, parent: QObject | None = None):
        super().__init__(parent)
        self._theme = theme
        self._mood = Mood.IDLE
        self._size_scale = 1.0
        self._motion_scale = 1.0
        self._running = True
        self._bubble_text = ""
        self._bubble_expanded = False
        self._bubble_max_lines = 4
        self._guide: GuideView | None = None
        self._card_open = False
        self._show_why = False
        self._dodged = False
        self._mouth_open = 0.0
        self._mic_level = 0.0

    # ---- state set from Python -------------------------------------------------------

    def set_mood(self, mood: Mood) -> None:
        if mood is not self._mood:
            self._mood = mood
            self.moodChanged.emit()

    def set_view(
        self,
        *,
        size_scale: float | None = None,
        motion_scale: float | None = None,
        running: bool | None = None,
        bubble_max_lines: int | None = None,
    ) -> None:
        before = (self._size_scale, self._motion_scale, self._running, self._bubble_max_lines)
        if size_scale is not None:
            self._size_scale = size_scale
        if motion_scale is not None:
            self._motion_scale = motion_scale
        if running is not None:
            self._running = running
        if bubble_max_lines is not None:
            self._bubble_max_lines = bubble_max_lines
        if before != (self._size_scale, self._motion_scale, self._running, self._bubble_max_lines):
            self.viewChanged.emit()

    def set_bubble(self, text: str, *, expanded: bool | None = None) -> None:
        if expanded is None:
            expanded = self._bubble_expanded if text else False
        if (text, expanded) != (self._bubble_text, self._bubble_expanded):
            self._bubble_text, self._bubble_expanded = text, expanded
            self.bubbleChanged.emit()

    def append_bubble(self, chunk: str) -> None:
        self.set_bubble(self._bubble_text + chunk)

    def set_guide(self, guide: GuideView | None, *, card_open: bool | None = None) -> None:
        self._guide = guide
        if guide is None:
            self._card_open = False
            self._show_why = False
        elif card_open is not None:
            self._card_open = card_open
        self.guideChanged.emit()

    def set_card_open(self, is_open: bool) -> None:
        if self._guide is not None and is_open != self._card_open:
            self._card_open = is_open
            self.guideChanged.emit()

    def set_show_why(self, show: bool) -> None:
        if show != self._show_why:
            self._show_why = show
            self.guideChanged.emit()

    def set_dodged(self, dodged: bool) -> None:
        if dodged != self._dodged:
            self._dodged = dodged
            self.dodgedChanged.emit()

    def set_voice(self, *, mouth_open: float | None = None, mic_level: float | None = None) -> None:
        if mouth_open is not None:
            self._mouth_open = min(1.0, max(0.0, mouth_open))
        if mic_level is not None:
            self._mic_level = min(1.0, max(0.0, mic_level))
        self.voiceChanged.emit()

    @property
    def current_mood(self) -> Mood:
        return self._mood

    @property
    def guide_view(self) -> GuideView | None:
        return self._guide

    @property
    def overlay_open(self) -> bool:
        """True while a bubble or the guide card is showing (blocks dodging)."""
        return bool(self._bubble_text) or self._card_open

    # ---- properties read by QML ------------------------------------------------------

    theme = Property(QObject, lambda self: self._theme, constant=True)
    mood = Property(str, lambda self: self._mood.value, notify=moodChanged)
    sizeScale = Property(float, lambda self: self._size_scale, notify=viewChanged)
    motionScale = Property(float, lambda self: self._motion_scale, notify=viewChanged)
    running = Property(bool, lambda self: self._running, notify=viewChanged)
    bubbleMaxLines = Property(int, lambda self: self._bubble_max_lines, notify=viewChanged)
    bubbleText = Property(str, lambda self: self._bubble_text, notify=bubbleChanged)
    bubbleExpanded = Property(bool, lambda self: self._bubble_expanded, notify=bubbleChanged)
    cardOpen = Property(bool, lambda self: self._card_open, notify=guideChanged)
    showWhy = Property(bool, lambda self: self._show_why, notify=guideChanged)
    dodged = Property(bool, lambda self: self._dodged, notify=dodgedChanged)
    mouthOpen = Property(float, lambda self: self._mouth_open, notify=voiceChanged)
    micLevel = Property(float, lambda self: self._mic_level, notify=voiceChanged)

    def _guide_qml(self) -> Any:
        return None if self._guide is None else self._guide.to_qml()

    guide = Property("QVariant", _guide_qml, notify=guideChanged)  # type: ignore[arg-type]

    def _pill_text(self) -> str:
        g = self._guide
        if g is None or self._card_open:
            return ""
        index = min(g.current, g.total - 1)
        return f"{index + 1}/{g.total} · {g.steps[index].title}"

    pillText = Property(str, _pill_text, notify=guideChanged)

    # ---- slots called by QML ---------------------------------------------------------

    @Slot(list)
    def setInputRects(self, rects: list[Any]) -> None:
        out: list[QRect] = []
        for r in rects:
            if isinstance(r, QRectF | QRect):
                rect = r.toAlignedRect() if isinstance(r, QRectF) else r
                if rect.isValid():
                    out.append(rect)
        self.inputRectsChanged.emit(out)

    @Slot()
    def startDrag(self) -> None:
        self.dragRequested.emit()

    @Slot(bool)
    def bodyHover(self, inside: bool) -> None:
        self.bodyHoverChanged.emit(inside)

    @Slot()
    def bodyPress(self) -> None:
        self.bodyPressed.emit()

    @Slot()
    def bodyClick(self) -> None:
        self.bodyClicked.emit()

    @Slot(str)
    def guideAction(self, name: str) -> None:
        if name in {"back", "skip", "done", "show", "why"}:
            self.guideActionRequested.emit(name)

    @Slot(str)
    def submitText(self, text: str) -> None:
        text = text.strip()
        if text:
            self.textSubmitted.emit(text[:4000])

    @Slot()
    def toggleBubble(self) -> None:
        self.set_bubble(self._bubble_text, expanded=not self._bubble_expanded)

    @Slot()
    def dismissBubble(self) -> None:
        self.set_bubble("")

    @Slot()
    def openCard(self) -> None:
        self.set_card_open(True)

    @Slot()
    def requestAsk(self) -> None:
        self.askRequested.emit()

    @Slot()
    def transientDone(self) -> None:
        self.transientFinished.emit()
