"""waddle-core: shared schemas and pure logic. No I/O, no Qt."""

from waddle_core.identity import APP_ID, CAPTURE_APP_ID, OVERLAY_WINDOW_TITLE, PENGUIN_WINDOW_TITLE
from waddle_core.mood import Mood

__all__ = [
    "APP_ID",
    "CAPTURE_APP_ID",
    "OVERLAY_WINDOW_TITLE",
    "PENGUIN_WINDOW_TITLE",
    "Mood",
]
