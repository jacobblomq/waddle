"""The additions to the senses for a question: a one-shot look and state listeners."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import assist_helpers as h

from waddle_core.privacy import BlindReason
from waddle_desktop.sensors.capture_client import Blind, Unchanged
from waddle_desktop.sensors.senses import EyesState


async def test_look_once_hands_the_frame_to_the_caller_and_keeps_none() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win())
    glimpse = await rig.senses.look_once()
    assert glimpse.look.status == "ok"
    assert glimpse.frame is not None and glimpse.frame.overview == h.JPEG
    assert glimpse.window_uuid == h.U1
    # The eyes panel is closed: nothing was shown and nothing is held (D-049).
    assert rig.frames == []
    assert rig.senses.state.look is not None and rig.senses.state.look.status == "ok"


async def test_look_once_always_asks_for_a_fresh_frame() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win())
    rig.senses.set_watching(True)  # the eyes panel already holds a frame of this window
    await rig.senses.look()
    await rig.senses.look_once()
    assert [c[3] for c in rig.eye.captures()][-1] is True  # force: never "unchanged"
    await rig.senses.stop()


async def test_look_once_also_shows_the_frame_while_the_eyes_panel_is_open() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win())
    rig.senses.set_watching(True)
    glimpse = await rig.senses.look_once()
    assert rig.frames and rig.frames[-1] is glimpse.frame
    await rig.senses.stop()


async def test_look_once_is_blind_when_the_gate_is_closed_and_starts_no_helper() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win(cls="KeePassXC", caption="Passwords"))
    assert rig.senses.state.blind is BlindReason.BLOCKED
    glimpse = await rig.senses.look_once()
    assert (glimpse.look.status, glimpse.look.detail) == ("blind", "blocked")
    assert glimpse.frame is None and rig.started == 0 and rig.frames == []


async def test_a_frame_for_a_window_that_became_blocked_is_not_handed_over() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win())
    rig.eye.gate = asyncio.Event()
    looking = asyncio.ensure_future(rig.senses.look_once())
    await h.until(rig.eye.captures)
    rig.senses.target_changed(h.win(caption="Private Browsing"))
    rig.eye.gate.set()
    glimpse = await looking
    assert glimpse.frame is None and glimpse.look.status == "blind"


async def test_a_helper_refusal_or_no_change_gives_no_frame() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win())
    rig.eye.results = [Blind("blocked"), Unchanged("0" * 16)]
    first = await rig.senses.look_once()
    second = await rig.senses.look_once()
    assert (first.look.status, first.look.detail, first.frame) == (
        "blind",
        "helper-blocked",
        None,
    )
    assert (second.look.status, second.frame) == ("unchanged", None)


async def test_listeners_hear_every_state_and_cannot_break_the_senses() -> None:
    rig = h.SensesRig()
    heard: list[EyesState] = []

    def broken(_state: EyesState) -> None:
        raise RuntimeError("CANARY-listener")

    rig.senses.add_listener(broken)
    rig.senses.add_listener(heard.append)
    with h.captured_logs() as logs:
        await rig.focus(h.win())
    assert heard == rig.states and heard[-1].identity is not None
    assert any("senses.listener_failed" in line for line in logs)
    assert "CANARY" not in "".join(logs)


async def test_a_look_once_frame_is_not_shown_once_the_eyes_panel_has_closed() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win())
    rig.senses.set_watching(True)
    rig.eye.gate = asyncio.Event()
    looking = asyncio.ensure_future(rig.senses.look_once())
    await h.until(rig.eye.captures)
    rig.senses.set_watching(False)  # the panel closed while the helper was busy
    rig.eye.gate.set()
    glimpse = await looking
    assert glimpse.frame is not None  # the question still gets its picture
    assert [f for f in rig.frames if f is not None] == []  # but nobody is shown it (D-049)
    await rig.senses.stop()
