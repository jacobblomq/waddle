"""The single object QML talks to. Lives on the UI thread; holds view state only."""

from __future__ import annotations

from typing import Any, Final

from PySide6.QtCore import Property, QObject, QRect, QRectF, QTimer, Signal, Slot

from waddle_core.guide import GuideView
from waddle_core.mood import Mood
from waddle_desktop.ui.theme_object import ThemeObject

# The brain caps an answer long before this; the bubble still never holds more.
MAX_BUBBLE_CHARS: Final = 24_000
# Streamed pieces are gathered for this long and shown in one go, so a fast answer lays the
# bubble out a few times a second and not once per token.
STREAM_FLUSH_MS: Final = 60
_CUT: Final = "…"


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
    stopRequested = Signal()  # Stop on a streaming answer, or the bubble closed mid-answer
    bubbleActionRequested = Signal()  # the one optional button under a bubble

    def __init__(self, theme: ThemeObject, parent: QObject | None = None):
        super().__init__(parent)
        self._theme = theme
        self._mood = Mood.IDLE
        self._size_scale = 1.0
        self._style = "minimal"
        self._motion_scale = 1.0
        self._running = True
        self._bubble_text = ""
        self._bubble_expanded = False
        self._bubble_max_lines = 4
        self._bubble_note = ""
        self._bubble_action = ""
        self._streaming = False
        self._placeholder = False  # the text shown is a stand-in until the first words arrive
        self._pending: list[str] = []
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(STREAM_FLUSH_MS)
        self._flush.timeout.connect(self._flush_stream)
        self._guide: GuideView | None = None
        self._card_open = False
        self._show_why = False
        self._dodged = False
        self._badge = False
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
        style: str | None = None,
        motion_scale: float | None = None,
        running: bool | None = None,
        bubble_max_lines: int | None = None,
    ) -> None:
        before = (self._size_scale, self._motion_scale, self._running, self._bubble_max_lines)
        if size_scale is not None:
            self._size_scale = size_scale
        if style is not None and style != self._style:
            self._style = style
            self.viewChanged.emit()
        if motion_scale is not None:
            self._motion_scale = motion_scale
        if running is not None:
            self._running = running
        if bubble_max_lines is not None:
            self._bubble_max_lines = bubble_max_lines
        if before != (self._size_scale, self._motion_scale, self._running, self._bubble_max_lines):
            self.viewChanged.emit()

    def _bubble_state(self) -> tuple[str, bool, str, str, bool]:
        return (
            self._bubble_text,
            self._bubble_expanded,
            self._bubble_note,
            self._bubble_action,
            self._streaming,
        )

    def set_bubble(
        self, text: str, *, expanded: bool | None = None, note: str = "", action: str = ""
    ) -> None:
        """Show one whole message (or none). Ends a streaming answer, if one was showing.

        ``note`` is a small line under the text; ``action`` is the label of one button.
        """
        before = self._bubble_state()
        if expanded is None:
            expanded = self._bubble_expanded if text else False
        self._pending.clear()
        self._flush.stop()
        self._streaming = self._placeholder = False
        self._bubble_text, self._bubble_expanded = text[:MAX_BUBBLE_CHARS], expanded
        self._bubble_note, self._bubble_action = (note, action) if text else ("", "")
        if before != self._bubble_state():
            self.bubbleChanged.emit()

    # A streamed answer: begin_stream, any number of push_delta, then end_stream.

    def begin_stream(self, placeholder: str = "") -> None:
        """A new answer is coming. ``placeholder`` shows until its first words arrive."""
        self.set_bubble(placeholder, expanded=False)
        self._streaming, self._placeholder = True, bool(placeholder)
        self.bubbleChanged.emit()

    def push_delta(self, piece: str) -> None:
        """The next piece of the answer. Shown with the others on the next flush."""
        if not self._streaming or not piece:
            return  # nothing is streaming (stopped, dismissed, finished): a late piece
        self._pending.append(piece)
        if not self._flush.isActive():
            self._flush.start()

    def _flush_stream(self) -> None:
        if not self._pending:
            return
        piece = "".join(self._pending)
        self._pending.clear()
        text = piece if self._placeholder else self._bubble_text + piece
        self._placeholder = False
        if len(text) > MAX_BUBBLE_CHARS:
            text = text[: MAX_BUBBLE_CHARS - 1] + _CUT
        if text != self._bubble_text:
            self._bubble_text = text
            self.bubbleChanged.emit()

    def end_stream(self, *, note: str = "", action: str = "", fallback: str = "") -> None:
        """The answer is over. What arrived stays; ``fallback`` shows if nothing did.

        An answer of blank lines only counts as nothing: it would be an empty-looking bubble.
        """
        if not self._streaming:
            return
        self._flush.stop()
        self._flush_stream()
        text = "" if self._placeholder else self._bubble_text
        self.set_bubble(text if text.strip() else fallback, note=note, action=action)

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def stream_has_text(self) -> bool:
        """Words of the answer are showing or about to (not just the placeholder)."""
        if not self._streaming:
            return False
        shown = "" if self._placeholder else self._bubble_text
        return bool(shown.strip()) or any(piece.strip() for piece in self._pending)

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

    def set_badge(self, badge: bool) -> None:
        """Small and faint, for when the focused app is fullscreen."""
        if badge != self._badge:
            self._badge = badge
            self.viewChanged.emit()

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
    style = Property(str, lambda self: self._style, notify=viewChanged)
    sizeScale = Property(float, lambda self: self._size_scale, notify=viewChanged)
    motionScale = Property(float, lambda self: self._motion_scale, notify=viewChanged)
    running = Property(bool, lambda self: self._running, notify=viewChanged)
    bubbleMaxLines = Property(int, lambda self: self._bubble_max_lines, notify=viewChanged)
    bubbleText = Property(str, lambda self: self._bubble_text, notify=bubbleChanged)
    bubbleExpanded = Property(bool, lambda self: self._bubble_expanded, notify=bubbleChanged)
    bubbleNote = Property(str, lambda self: self._bubble_note, notify=bubbleChanged)
    bubbleAction = Property(str, lambda self: self._bubble_action, notify=bubbleChanged)
    bubbleBusy = Property(bool, lambda self: self._streaming, notify=bubbleChanged)
    cardOpen = Property(bool, lambda self: self._card_open, notify=guideChanged)
    showWhy = Property(bool, lambda self: self._show_why, notify=guideChanged)
    dodged = Property(bool, lambda self: self._dodged, notify=dodgedChanged)
    badge = Property(bool, lambda self: self._badge, notify=viewChanged)
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
        if self._bubble_text:
            # Only this flag: a streaming answer keeps streaming, a note stays.
            self._bubble_expanded = not self._bubble_expanded
            self.bubbleChanged.emit()

    @Slot()
    def dismissBubble(self) -> None:
        was_streaming = self._streaming
        self.set_bubble("")
        if was_streaming:
            self.stopRequested.emit()  # closing the bubble mid-answer also stops the answer

    @Slot()
    def stopStream(self) -> None:
        if self._streaming:
            self.stopRequested.emit()

    @Slot()
    def bubbleActionClick(self) -> None:
        if self._bubble_action:
            self.bubbleActionRequested.emit()

    @Slot()
    def openCard(self) -> None:
        self.set_card_open(True)

    @Slot()
    def requestAsk(self) -> None:
        self.askRequested.emit()

    @Slot()
    def transientDone(self) -> None:
        self.transientFinished.emit()
