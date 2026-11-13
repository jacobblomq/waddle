"""What may be said about the window in front: only what the gate allows, cleaned."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import assist_helpers as h

from waddle_core import capabilities as caps
from waddle_core.privacy import BlindReason
from waddle_desktop.assist import sight
from waddle_desktop.sensors.senses import EyesState, Look

CANARY = "CANARY-7c1e"
OPEN = EyesState(blind=None, window=h.win(caption=f"{CANARY} report"), session=h.UNLOCKED)


def test_an_allowed_window_is_described_with_a_clean_title_and_pixel_size() -> None:
    context = sight.window_context(OPEN)
    assert context is not None
    assert (context.resource_class, context.title) == ("GoodApp", f"{CANARY} report")
    assert (context.width, context.height, context.scale) == (800, 600, 1.0)
    framed = sight.window_context(OPEN, h.frame())
    assert framed is not None and (framed.width, framed.height) == (1600, 1200)


@pytest.mark.parametrize("reason", list(BlindReason))
def test_a_window_the_gate_refuses_is_not_described_at_all(reason: BlindReason) -> None:
    state = replace(OPEN, blind=reason)
    assert sight.window_context(state) is None
    assert sight.window_context(state, h.frame()) is None
    assert sight.gate_reason(state) == reason.value


def test_no_window_is_no_context() -> None:
    assert sight.window_context(replace(OPEN, window=None)) is None


def test_odd_window_numbers_do_not_raise() -> None:
    odd = replace(h.win(), scale=float("nan"))
    assert sight.window_context(replace(OPEN, window=odd)) is None
    huge = replace(h.win(), scale=64.0)
    context = sight.window_context(replace(OPEN, window=huge))
    assert context is not None and context.scale == 1.0 and context.width <= 100_000


@pytest.mark.parametrize(
    ("look", "reason"),
    [
        (Look("blind", "locked"), "locked"),
        (Look("blind", "helper-locked"), "locked"),
        (Look("blind", "helper-blocked"), "blocked"),
        (Look("blind", "helper-latched"), "paused"),
        (Look("blind", "helper-not_found"), "no-window"),
        (Look("blind", "stale"), "no-window"),
        (Look("blind", f"{CANARY}"), "capture-failed"),  # an unknown detail is never passed on
        (Look("unavailable", "helper-missing"), "helper-missing"),
        (Look("unavailable", "CaptureTimeoutError"), "helper-missing"),
        (Look("error", "lock_unknown"), "unknown-lock"),
        (Look("error", "kwin_failed"), "capture-failed"),
        (Look("unchanged"), "capture-failed"),
        (Look("skipped", "watching-level"), "capture-failed"),
    ],
)
def test_a_look_without_a_picture_becomes_a_short_known_reason(look: Look, reason: str) -> None:
    assert sight.no_frame_reason(look, OPEN) == reason


def test_the_gate_says_why_before_anything_the_look_said() -> None:
    paused = replace(OPEN, blind=BlindReason.PAUSED)
    assert sight.no_frame_reason(Look("unavailable", "helper-missing"), paused) == "paused"


def test_capture_capability_is_about_the_helper_not_the_window() -> None:
    blocked = replace(OPEN, blind=BlindReason.BLOCKED)
    assert sight.capture_capability(blocked, helper_installed=True) == (True, "")
    assert sight.capture_capability(OPEN, helper_installed=False) == (
        False,
        caps.CAPTURE_HELPER_MISSING,
    )
    for change, code in [
        ({"kwin": False}, caps.CAPTURE_NO_KWIN),
        ({"lock_check": False}, caps.CAPTURE_LOCK_UNKNOWN),
        ({"config_ok": False}, caps.CAPTURE_CONFIG_UNREADABLE),
    ]:
        state = replace(OPEN, helper=replace(h.HELLO, **change))  # type: ignore[arg-type]
        assert sight.capture_capability(state, helper_installed=True) == (False, code)
    failed = replace(OPEN, look=Look("unavailable", "helper-spawn_failed"))
    assert sight.capture_capability(failed, helper_installed=True) == (
        False,
        caps.CAPTURE_HELPER_FAILED,
    )
