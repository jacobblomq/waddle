"""What may be said about the focused window, and why Waddle cannot see when he cannot.

Pure helpers over the senses' ``EyesState``. They are the one place that decides which window
facts leave the desktop process: nothing about a window is passed on unless the privacy gate
allows looking at it (ARCHITECTURE §1 I4), and a window title is always cleaned and capped.
"""

from __future__ import annotations

from typing import Final

from pydantic import ValidationError

from waddle_core import capabilities as caps
from waddle_core.link import MAX_TITLE_CHARS, WindowContext
from waddle_core.privacy import BlindReason
from waddle_core.untrusted import clean
from waddle_desktop.sensors.capture_client import Frame
from waddle_desktop.sensors.senses import EyesState, Look

# Reasons sent to the brain as ``Ask.blind_reason``: a ``BlindReason`` value or one of these.
HELPER_MISSING: Final = "helper-missing"
CAPTURE_FAILED: Final = "capture-failed"  # the helper is there but this look did not work

HARD_REASONS: Final = frozenset(
    {
        BlindReason.LOCKED.value,
        BlindReason.UNKNOWN_LOCK.value,
        BlindReason.PAUSED.value,
        BlindReason.BLOCKED.value,
    }
)
_GATE_VALUES: Final = frozenset(reason.value for reason in BlindReason)
# What the capture helper and the senses say when a look gave no picture.
_LOOK_DETAILS: Final = {
    "helper-locked": BlindReason.LOCKED.value,
    "helper-blocked": BlindReason.BLOCKED.value,
    "helper-latched": BlindReason.PAUSED.value,  # the latch is how a pause reaches the helper
    "helper-not_found": BlindReason.NO_WINDOW.value,
    "stale": BlindReason.NO_WINDOW.value,  # the window changed while Waddle was looking
}
_CLASS_CHARS: Final = 128
_MAX_SIDE: Final = 100_000


def one_line(text: object, limit: int) -> str:
    """Untrusted text as one clean line of at most ``limit`` characters."""
    if not isinstance(text, str):
        return ""
    return " ".join(clean(text[: limit * 4], limit).split())


def gate_reason(state: EyesState) -> str | None:
    """Why the gate forbids looking at the current window, or None if it allows it."""
    return None if state.blind is None else state.blind.value


def no_frame_reason(look: Look, state: EyesState) -> str:
    """The blind reason for a look that produced no picture."""
    if state.blind is not None:
        return state.blind.value
    if look.status == "blind":
        if look.detail in _GATE_VALUES:
            return look.detail
        return _LOOK_DETAILS.get(look.detail, CAPTURE_FAILED)
    if look.status == "unavailable":
        return HELPER_MISSING
    if look.status == "error" and look.detail == "lock_unknown":
        return BlindReason.UNKNOWN_LOCK.value
    return CAPTURE_FAILED


def capture_capability(state: EyesState, *, helper_installed: bool) -> tuple[bool, str]:
    """(can the helper be used, a ``waddle_core.capabilities`` CAPTURE_* code if not).

    This is about the helper, not about the window in front: a blocked window or a pause is
    not a missing capability.
    """
    if not helper_installed:
        return False, caps.CAPTURE_HELPER_MISSING
    hello = state.helper
    if hello is not None:
        if not hello.kwin:
            return False, caps.CAPTURE_NO_KWIN
        if not hello.lock_check:
            return False, caps.CAPTURE_LOCK_UNKNOWN
        if not hello.config_ok:
            return False, caps.CAPTURE_CONFIG_UNREADABLE
    if state.look is not None and state.look.status == "unavailable":
        return False, caps.CAPTURE_HELPER_FAILED
    return True, ""


def window_context(state: EyesState, frame: Frame | None = None) -> WindowContext | None:
    """The window as the link describes it, or None unless the gate allows looking at it."""
    window = state.window
    if state.blind is not None or window is None:
        return None
    try:
        scale = float(window.scale)
        if frame is not None:
            width, height = frame.width, frame.height
        else:
            width = round(window.client.width * scale)
            height = round(window.client.height * scale)
        return WindowContext(
            resource_class=one_line(window.resource_class, _CLASS_CHARS),
            title=one_line(window.caption, MAX_TITLE_CHARS),
            width=min(max(int(width), 0), _MAX_SIDE),
            height=min(max(int(height), 0), _MAX_SIDE),
            scale=scale if 0 < scale <= 8 else 1.0,
            fullscreen=bool(window.fullscreen),
            xwayland=window.xwayland if isinstance(window.xwayland, bool) else None,
        )
    except (ValidationError, ValueError, TypeError, OverflowError):
        return None
