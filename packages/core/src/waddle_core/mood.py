"""The penguin's animated states (VISION §4.1)."""

from enum import StrEnum


class Mood(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    WATCHING = "watching"
    THINKING = "thinking"
    SPEAKING = "speaking"
    CELEBRATE = "celebrate"
    ALERT = "alert"
    CONFUSED = "confused"
    EYES_CLOSED = "eyes-closed"
    SLEEPING = "sleeping"
    NAPPING_BRAIN = "napping-brain"

    @property
    def is_transient(self) -> bool:
        """Transient moods play once and then fall back to the previous steady mood."""
        return self in {Mood.CELEBRATE, Mood.ALERT}


STEADY_MOODS = frozenset(m for m in Mood if not m.is_transient)
