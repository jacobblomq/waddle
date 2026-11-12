"""The senses controller: the gate decides, the helper obeys, stale answers are dropped."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import replace

import pytest

from waddle_core.config import WatchingConfig
from waddle_core.privacy import BlindReason, Blocklist
from waddle_core.watching import CaptureScheduler, WatchPolicy
from waddle_core.window import Geometry, WindowInfo
from waddle_desktop.identity.cache import UNKNOWN_IDENTITY, AppIdentity
from waddle_desktop.kwin.types import Popup, PopupSet
from waddle_desktop.sensors import senses as senses_mod
from waddle_desktop.sensors.capture_client import (
    Blind,
    CaptureClosedError,
    CaptureError,
    CaptureResult,
    Frame,
    Hello,
    Unchanged,
)
from waddle_desktop.sensors.helper_proc import HelperUnavailableError
from waddle_desktop.sensors.senses import EyesState, Senses
from waddle_desktop.sensors.session import SessionSnapshot

UNLOCKED = SessionSnapshot(locked=False, lock_known=True, dnd=False, idle=False)
LOCKED = replace(UNLOCKED, locked=True)
G = Geometry(0, 0, 800, 600)
U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
U3 = "33333333-3333-3333-3333-333333333333"


def win(
    uuid: str = U1,
    *,
    cls: str = "blender",
    caption: str = "Blender",
    pid: int = 10,
    own: bool = False,
) -> WindowInfo:
    return WindowInfo(uuid, caption, cls, cls, cls, pid, G, G, "DP-1", 1.0, False, False, own)


def frame() -> Frame:
    return Frame("0" * 16, 1.0, 800, 600, b"JPEG", (800, 600), b"PNG", (0, 0, 512, 512), 7, 0)


HELLO = Hello("0.2.0", 1, True, 4, "restricted", True, True, False)


class FakeEye:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.results: list[CaptureResult | Exception] = []
        self.closed = False
        self.gate: asyncio.Event | None = None

    async def hello(self) -> Hello:
        self.calls.append(("hello",))
        return HELLO

    async def set_blind(self, on: bool) -> None:
        self.calls.append(("blind", on))

    async def forget(self) -> None:
        self.calls.append(("forget",))

    async def capture_window(
        self, uuid: str, *, popups: Iterable[str] = (), force: bool = False
    ) -> CaptureResult:
        self.calls.append(("capture", uuid, tuple(popups), force))
        if self.gate is not None:
            await self.gate.wait()
        result = self.results.pop(0) if self.results else frame()
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True

    def captures(self) -> list[tuple[object, ...]]:
        return [c for c in self.calls if c[0] == "capture"]


class Rig:
    def __init__(self) -> None:
        self.eyes: list[FakeEye] = []
        self.states: list[EyesState] = []
        self.frames: list[Frame | None] = []
        self.unavailable: str | None = None
        self.identity: AppIdentity = UNKNOWN_IDENTITY
        self.next_eye: FakeEye | None = None
        self.senses = Senses(
            blocklist=Blocklist(classes=("keepassxc",), titles=("private browsing",)),
            scheduler=CaptureScheduler(WatchPolicy.from_config(WatchingConfig())),
            eye_factory=self._factory,
            identify=lambda _w: self.identity,
            on_state=self.states.append,
            on_frame=self.frames.append,
        )

    async def _factory(self) -> FakeEye:
        if self.unavailable:
            raise HelperUnavailableError(self.unavailable)
        eye = self.next_eye or FakeEye()
        self.next_eye = None
        self.eyes.append(eye)
        return eye

    @property
    def eye(self) -> FakeEye:
        return self.eyes[-1]

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(senses_mod, "LOOK_DEBOUNCE", 0.0)


async def test_before_the_lock_state_is_known_waddle_is_blind_and_starts_no_helper() -> None:
    rig = Rig()
    rig.senses.window_activated(win())
    assert rig.senses.state.blind is BlindReason.UNKNOWN_LOCK
    look = await rig.senses.look()
    assert (look.status, look.detail) == ("blind", "unknown-lock")
    assert rig.eyes == []


async def test_an_allowed_window_is_captured_and_the_frame_is_delivered() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    assert rig.senses.state.blind is None
    look = await rig.senses.look()
    assert (look.status, look.width, look.height) == ("ok", 800, 600)
    assert rig.frames == [frame()]
    assert rig.eye.captures() == [("capture", U1, (), True)]
    assert rig.senses.state.helper == HELLO


@pytest.mark.parametrize(
    ("window", "session", "paused", "reason"),
    [
        (win(cls="org.keepassxc.KeePassXC"), UNLOCKED, False, "blocked"),
        (win(caption="Firefox — Private Browsing"), UNLOCKED, False, "blocked"),
        (win(), LOCKED, False, "locked"),
        (win(), UNLOCKED, True, "paused"),
        (win(), replace(UNLOCKED, lock_known=False), False, "unknown-lock"),
    ],
)
async def test_a_blind_gate_means_the_helper_is_never_asked(
    window: WindowInfo, session: SessionSnapshot, paused: bool, reason: str
) -> None:
    rig = Rig()
    rig.senses.session_changed(session)
    rig.senses.set_paused(paused)
    rig.senses.window_activated(window)
    assert rig.senses.state.eyes_closed
    look = await rig.senses.look()
    assert (look.status, look.detail) == ("blind", reason)
    assert rig.eyes == [] and rig.frames == []


async def test_going_blind_drops_the_frame_and_latches_the_helper() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    await rig.senses.look()
    rig.senses.session_changed(LOCKED)
    await rig.settle()
    assert rig.frames[-1] is None
    assert rig.eye.calls[-2:] == [("blind", True), ("forget",)]
    assert rig.senses.state.look is None

    rig.senses.session_changed(UNLOCKED)
    await rig.senses.look()
    assert rig.eye.calls[-2][0:2] == ("blind", False), "the latch is lifted only to look again"
    assert isinstance(rig.frames[-1], Frame)


async def test_a_title_that_becomes_blocked_closes_the_eyes() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win(cls="firefox", caption="News"))
    await rig.senses.look()
    rig.senses.target_changed(win(cls="firefox", caption="Private Browsing"))
    await rig.settle()
    assert rig.senses.state.blind is BlindReason.BLOCKED
    assert rig.frames[-1] is None
    assert ("blind", True) in rig.eye.calls


async def test_a_frame_that_arrives_after_the_gate_closed_is_discarded() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    eye = rig.next_eye = FakeEye()
    eye.gate = asyncio.Event()
    task = asyncio.ensure_future(rig.senses.look())
    await rig.settle()
    assert eye.captures(), "the capture is in flight"
    rig.senses.session_changed(LOCKED)
    eye.gate.set()
    look = await task
    assert (look.status, look.detail) == ("blind", "locked")
    assert not any(isinstance(f, Frame) for f in rig.frames)


async def test_a_frame_for_a_window_that_is_no_longer_the_target_is_discarded() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    eye = rig.next_eye = FakeEye()
    eye.gate = asyncio.Event()
    task = asyncio.ensure_future(rig.senses.look())
    await rig.settle()
    rig.senses.window_activated(win(U2, cls="kate", pid=11))
    eye.gate.set()
    look = await task
    assert (look.status, look.detail) == ("blind", "stale")
    assert not any(isinstance(f, Frame) for f in rig.frames)


async def test_wadddles_own_windows_never_become_the_target() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    rig.senses.window_activated(win(U2, cls="io.github.jacobblomq.Waddle", own=True))
    window = rig.senses.state.window
    assert window is not None and window.uuid == U1


async def test_popups_of_the_target_are_included_and_others_are_not() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    rig.senses.popups_changed(PopupSet(U1, (Popup(U3, G),)))
    await rig.senses.look()
    assert rig.eye.captures()[-1][2] == (U3,)
    rig.senses.window_activated(win(U2, cls="kate", pid=11))
    await rig.senses.look()
    assert rig.eye.captures()[-1][1:3] == (U2, ()), "another window's popups are not sent"


async def test_helper_results_other_than_a_frame() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    await rig.senses.look()
    rig.eye.results = [Unchanged("0" * 16), Blind("blocked"), CaptureError("not_authorized")]
    assert (await rig.senses.look()).status == "unchanged"
    assert isinstance(rig.frames[-1], Frame), "an unchanged window keeps its frame"
    look = await rig.senses.look()
    assert (look.status, look.detail) == ("blind", "helper-blocked")
    assert rig.frames[-1] is None, "the helper's refusal drops what we had"
    look = await rig.senses.look()
    assert (look.status, look.detail) == ("error", "not_authorized")


async def test_an_idle_exited_helper_is_restarted_once() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    await rig.senses.look()
    first = rig.eye
    first.results = [CaptureClosedError()]
    look = await rig.senses.look()
    assert look.status == "ok" and first.closed and len(rig.eyes) == 2

    rig.eye.results = [CaptureClosedError()]
    dead = rig.next_eye = FakeEye()
    dead.results = [CaptureClosedError()]
    look = await rig.senses.look()
    assert (look.status, look.detail) == ("unavailable", "helper-exited")


async def test_a_missing_helper_is_reported_not_raised() -> None:
    rig = Rig()
    rig.unavailable = "missing"
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    look = await rig.senses.look()
    assert (look.status, look.detail) == ("unavailable", "helper-missing")


async def test_the_app_is_identified_once_per_process_and_timed() -> None:
    rig = Rig()
    calls: list[int] = []
    known = replace(UNKNOWN_IDENTITY, name="blender")

    def identify(window: WindowInfo) -> AppIdentity:
        calls.append(window.pid)
        return known

    rig.senses._identify_fn = identify
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if rig.senses.state.identity is not None:
            break
    state = rig.senses.state
    assert state.identity == known and state.identify_ms is not None
    rig.senses.window_activated(win(caption="Blender — scene"))
    rig.senses.window_activated(win(U2, caption="Preferences"))
    await asyncio.sleep(0.05)
    assert calls == [10], "same process: no second lookup"


async def test_nothing_is_captured_unless_someone_is_watching(fast: None) -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    await asyncio.sleep(0.02)
    assert rig.eyes == [], "focus changes alone never trigger a capture"

    rig.senses.set_watching(True)
    await asyncio.sleep(0.02)
    assert len(rig.eye.captures()) == 1
    rig.senses.window_activated(win(U2, cls="kate", pid=11))
    await asyncio.sleep(0.02)
    assert rig.eye.captures()[-1][1] == U2

    rig.senses.set_watching(False)
    assert rig.frames[-1] is None, "closing the panel drops the frame"
    rig.senses.window_activated(win())
    await asyncio.sleep(0.02)
    assert len(rig.eye.captures()) == 2


async def test_stop_closes_the_helper_and_goes_quiet() -> None:
    rig = Rig()
    rig.senses.session_changed(UNLOCKED)
    rig.senses.window_activated(win())
    await rig.senses.look()
    await rig.senses.stop()
    assert rig.eye.closed and rig.frames[-1] is None
    count = len(rig.states)
    rig.senses.session_changed(LOCKED)
    assert len(rig.states) == count
