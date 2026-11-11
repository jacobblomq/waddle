"""Mock data for Phase 1: there is no brain yet, so the demo driver plays both parts.

Everything here is clearly labelled as a demo in what Waddle says; nothing pretends to see
the screen.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer

from waddle_core.guide import GuideView, mock_guide
from waddle_core.mood import STEADY_MOODS, Mood
from waddle_desktop.ui.bridge import Bridge

_ORDER = [m for m in Mood if m in STEADY_MOODS]
_GREETING = (
    "Hi, I'm Waddle! This is demo mode: I can't see your screen or answer questions yet. "
    "Click me to open a sample guide, drag me anywhere, or use my tray icon to try my moods."
)


def advance(guide: GuideView, action: str) -> GuideView:
    """Apply a guide-card button to the mock guide."""
    last = guide.total - 1
    current = min(guide.current, last)
    steps = list(guide.steps)
    if action == "back":
        current = max(0, current - 1)
        steps[current] = steps[current].model_copy(update={"done": False})
    elif action in {"skip", "done"}:
        if action == "done":
            steps[current] = steps[current].model_copy(update={"done": True})
        current = min(last, current + 1)
    return guide.model_copy(update={"steps": tuple(steps), "current": current})


class DemoDriver(QObject):
    def __init__(self, bridge: Bridge, parent: QObject | None = None):
        super().__init__(parent)
        self._bridge = bridge
        self._words: list[str] = []
        self._stream = QTimer(self)
        self._stream.setInterval(45)
        self._stream.timeout.connect(self._next_word)
        bridge.guideActionRequested.connect(self._on_action)
        bridge.textSubmitted.connect(self._on_text)

    def greet(self) -> None:
        self.say(_GREETING)

    def say(self, text: str) -> None:
        """Stream text into the bubble word by word, the way model output will arrive."""
        self._bridge.set_bubble("")
        self._words = text.split(" ")
        self._stream.start()

    def _next_word(self) -> None:
        if not self._words:
            self._stream.stop()
            return
        word = self._words.pop(0)
        self._bridge.append_bubble(word if not self._bridge.property("bubbleText") else " " + word)

    def handle(self, name: str) -> None:
        if name == "next mood":
            mood = self._bridge.current_mood
            index = _ORDER.index(mood) if mood in _ORDER else -1
            self._bridge.set_mood(_ORDER[(index + 1) % len(_ORDER)])
        elif name == "say something":
            self.greet()
        elif name == "toggle guide":
            self.toggle_guide()
        elif name in {"celebrate", "alert"}:
            self._bridge.set_mood(Mood(name))

    def toggle_guide(self) -> None:
        if self._bridge.guide_view is None:
            self._bridge.set_bubble("")
            self._bridge.set_guide(mock_guide(), card_open=True)
        else:
            self._bridge.set_guide(None)

    def _on_action(self, action: str) -> None:
        guide = self._bridge.guide_view
        if guide is None:
            return
        if action == "why":
            self._bridge.set_show_why(not bool(self._bridge.property("showWhy")))
            return
        if action == "show":
            self._bridge.set_card_open(False)
            self.say(
                "In demo mode I can't point at things yet. That arrives with the guide engine."
            )
            return
        was_last = guide.current >= guide.total - 1
        self._bridge.set_show_why(False)
        self._bridge.set_guide(advance(guide, action))
        if action == "done":
            self._bridge.set_mood(Mood.CELEBRATE)
            if was_last:
                self._bridge.set_guide(None)
                self.say("That was the last step of the sample guide. Nicely done!")

    def _on_text(self, text: str) -> None:
        self._bridge.set_mood(Mood.CONFUSED)
        self.say("I can't answer yet: my brain arrives in a later phase. (Demo mode.)")
