"""Waddle's senses, joined up: what is focused, may he look, and what did he last see.

One object owns the answer to "may Waddle look right now?" (the privacy gate) and is the only
caller of the capture helper. Everything here runs on the I/O thread's asyncio loop; the two
callbacks are invoked on that thread and must be forwarded to the UI as queued signals.

Rules this module keeps (ARCHITECTURE §1, I3/I4):

- A capture is requested only for the current target window, only after the gate allows it,
  and the result is thrown away if the gate or the target changed while it was in flight.
- On a hard blind (locked, lock state unknown, paused, blocked window) the last frame is
  dropped, the helper is latched and told to forget its change memory.
- Frames are bytes in memory. Nothing here writes them anywhere, and no window text is logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol

from waddle_core.privacy import Allow, BlindReason, Blocklist, SessionState, gate
from waddle_core.watching import CaptureScheduler, Trigger
from waddle_core.window import WindowInfo
from waddle_desktop import log
from waddle_desktop.identity.cache import AppIdentity
from waddle_desktop.kwin.types import PopupSet
from waddle_desktop.sensors.capture_client import (
    Blind,
    CaptureClient,
    CaptureClientError,
    CaptureClosedError,
    CaptureError,
    CaptureResult,
    Frame,
    Hello,
    Unchanged,
)
from waddle_desktop.sensors.helper_proc import HelperProcess, HelperUnavailableError, start_helper
from waddle_desktop.sensors.session import UNKNOWN, SessionSnapshot

HARD_BLIND: Final = frozenset(
    {BlindReason.LOCKED, BlindReason.UNKNOWN_LOCK, BlindReason.PAUSED, BlindReason.BLOCKED}
)
IDENTIFY_TIMEOUT: Final = 2.0
LOOK_DEBOUNCE: Final = 0.25
MAX_POPUPS: Final = 8


class Eye(Protocol):
    """The capture helper, as the senses use it."""

    async def hello(self) -> Hello: ...
    async def set_blind(self, on: bool) -> None: ...
    async def forget(self) -> None: ...
    async def capture_window(
        self, uuid: str, *, popups: Iterable[str] = (), force: bool = False
    ) -> CaptureResult: ...
    def close(self) -> None: ...


class HelperEye:
    """A running ``waddle-capture`` process and the client talking to it."""

    def __init__(self, process: HelperProcess):
        self._process = process
        self._client = CaptureClient(process.sock)

    async def hello(self) -> Hello:
        return await self._client.hello()

    async def set_blind(self, on: bool) -> None:
        await self._client.set_blind(on)

    async def forget(self) -> None:
        await self._client.forget()

    async def capture_window(
        self, uuid: str, *, popups: Iterable[str] = (), force: bool = False
    ) -> CaptureResult:
        return await self._client.capture_window(uuid, popups=popups, force=force)

    def close(self) -> None:
        self._client.close()
        self._process.stop(timeout=0.5)


@dataclass(frozen=True)
class Look:
    """The outcome of the last attempt to look. Carries no pixels and no window text."""

    status: str  # ok | unchanged | blind | error | unavailable | skipped
    detail: str = ""  # blind reason, error code, or why the helper is unavailable
    at: float = 0.0  # wall-clock seconds
    width: int = 0
    height: int = 0
    elapsed_ms: int = 0
    popups: int = 0


@dataclass(frozen=True)
class EyesState:
    blind: BlindReason | None = BlindReason.UNKNOWN_LOCK  # None: Waddle may look
    window: WindowInfo | None = None
    identity: AppIdentity | None = None
    identify_ms: float | None = None
    session: SessionSnapshot = UNKNOWN
    paused: bool = False
    helper: Hello | None = None
    look: Look | None = None

    @property
    def eyes_closed(self) -> bool:
        return self.blind in HARD_BLIND


class Senses:
    def __init__(
        self,
        *,
        blocklist: Blocklist,
        scheduler: CaptureScheduler,
        eye_factory: Callable[[], Awaitable[Eye]],
        identify: Callable[[WindowInfo], AppIdentity],
        on_state: Callable[[EyesState], None],
        on_frame: Callable[[Frame | None], None],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._blocklist = blocklist
        self._scheduler = scheduler
        self._eye_factory = eye_factory
        self._identify_fn = identify
        self._on_state = on_state
        self._on_frame = on_frame
        self._clock = clock
        self._state = EyesState()
        self._popups: PopupSet | None = None
        self._eye: Eye | None = None
        self._latched = False
        self._watching = False
        self._frame_uuid: str | None = None
        self._identified_pid: int | None = None
        self._look_lock = asyncio.Lock()
        self._look_handle: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[object]] = set()
        self._stopped = False

    @property
    def state(self) -> EyesState:
        return self._state

    # ---- inputs (call on the loop) -----------------------------------------------------

    def window_activated(self, window: WindowInfo) -> None:
        """A window became active. Waddle's own windows never become the target."""
        if window.is_own:
            return
        started = self._clock()
        changed = self._state.window is None or self._state.window.uuid != window.uuid
        if changed:
            self._popups = None
        if window.pid != self._identified_pid:
            self._identified_pid = window.pid
            self._update(window=window, identity=None, identify_ms=None)
            self._spawn(self._identify(window, started))
        else:
            self._update(window=window)
        self._auto_look()

    def target_changed(self, window: WindowInfo) -> None:
        """The target's title, geometry or fullscreen state changed."""
        current = self._state.window
        if window.is_own or current is None or current.uuid != window.uuid:
            return
        self._update(window=window)
        self._auto_look()

    def popups_changed(self, popups: PopupSet) -> None:
        self._popups = popups
        self._auto_look()

    def session_changed(self, session: SessionSnapshot) -> None:
        self._update(session=session)

    def set_paused(self, paused: bool) -> None:
        self._update(paused=paused)

    def set_watching(self, watching: bool) -> None:
        """While something is showing Waddle's view (the eyes panel), keep it current."""
        self._watching = watching
        if watching:
            self._auto_look()
        else:
            self._cancel_auto_look()
            self._drop_frame()

    async def stop(self) -> None:
        self._cancel_auto_look()
        self._drop_frame()
        self._stopped = True
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._drop_eye()

    # ---- the gate ------------------------------------------------------------------------

    def _blind_reason(self, state: EyesState) -> BlindReason | None:
        session = SessionState(
            locked=state.session.locked,
            paused=state.paused,
            idle=state.session.idle,
            lock_known=state.session.lock_known,
        )
        decision = gate(state.window, session, self._blocklist)
        return None if isinstance(decision, Allow) else decision.reason

    def _update(self, **changes: object) -> None:
        state = replace(self._state, **changes)  # type: ignore[arg-type]
        state = replace(state, blind=self._blind_reason(state))
        was_closed = self._state.eyes_closed
        self._state = state
        if state.eyes_closed and not was_closed:
            log.event("eyes.closed", reason=state.blind.value if state.blind else "")
            self._go_blind()
        elif was_closed and not state.eyes_closed:
            log.event("eyes.opened")
        elif state.eyes_closed and state.blind is BlindReason.BLOCKED:
            self._drop_frame()
        self._emit()

    def _emit(self) -> None:
        if not self._stopped:
            self._on_state(self._state)

    def _go_blind(self) -> None:
        self._cancel_auto_look()
        self._drop_frame()
        self._state = replace(self._state, look=None)
        if self._eye is not None and not self._latched:
            self._latched = True
            self._spawn(self._latch(self._eye))

    async def _latch(self, eye: Eye) -> None:
        try:
            await eye.set_blind(True)
            await eye.forget()
        except CaptureClientError:
            # A helper we cannot latch is a helper we stop.
            if self._eye is eye:
                self._drop_eye()

    def _drop_frame(self) -> None:
        if self._frame_uuid is not None:
            self._frame_uuid = None
            if not self._stopped:
                self._on_frame(None)

    def _drop_eye(self) -> None:
        eye, self._eye = self._eye, None
        self._latched = False
        if eye is not None:
            eye.close()

    # ---- identity ------------------------------------------------------------------------

    async def _identify(self, window: WindowInfo, started: float) -> None:
        loop = asyncio.get_running_loop()
        try:
            identity = await asyncio.wait_for(
                loop.run_in_executor(None, self._identify_fn, window), IDENTIFY_TIMEOUT
            )
        except TimeoutError:
            log.warning("identity.timeout")
            return
        except Exception as exc:
            log.warning("identity.failed", error=type(exc).__name__)
            return
        elapsed = (self._clock() - started) * 1000
        log.event(
            "identity.resolved",
            ms=round(elapsed, 1),
            packaging=identity.packaging.value,
            source=identity.source,
            versioned=identity.version is not None,
        )
        current = self._state.window
        if current is not None and current.pid == window.pid:
            self._update(identity=identity, identify_ms=elapsed)

    # ---- looking -------------------------------------------------------------------------

    def _auto_look(self) -> None:
        if not self._watching or self._stopped or self._state.blind is not None:
            return
        self._cancel_auto_look()
        loop = asyncio.get_running_loop()
        self._look_handle = loop.call_later(LOOK_DEBOUNCE, self._auto_look_fire)

    def _auto_look_fire(self) -> None:
        self._look_handle = None
        self._spawn(self.look())

    def _cancel_auto_look(self) -> None:
        if self._look_handle is not None:
            self._look_handle.cancel()
            self._look_handle = None

    async def look(self, trigger: Trigger = Trigger.USER_ASK, *, force: bool = False) -> Look:
        """Look at the target window once, if the gate and the watching level allow it."""
        async with self._look_lock:
            look = await self._look(trigger, force)
            look = replace(look, at=time.time())
            if look.status in {"ok", "unchanged"} or not self._state.eyes_closed:
                self._state = replace(self._state, look=look)
            self._emit()
            log.event(
                "look",
                status=look.status,
                detail=look.detail,
                w=look.width,
                h=look.height,
                ms=look.elapsed_ms,
                popups=look.popups,
            )
            return look

    async def _look(self, trigger: Trigger, force: bool) -> Look:
        state = self._state
        window = state.window
        if state.blind is not None or window is None:
            return Look("blind", state.blind.value if state.blind else "no-window")
        versioned = state.identity is not None and state.identity.version is not None
        if not self._scheduler.request(
            trigger,
            int(self._clock() * 1000),
            guide_active=False,
            version_known=versioned,
            window_uuid=window.uuid,
        ):
            return Look("skipped", "watching-level")
        popups: tuple[str, ...] = ()
        if self._popups is not None and self._popups.target == window.uuid:
            popups = tuple(p.uuid for p in self._popups.popups[:MAX_POPUPS])
        force = force or self._frame_uuid != window.uuid

        result: CaptureResult | None = None
        for attempt in (0, 1):  # the helper exits when idle; one respawn is normal
            unavailable = await self._ensure_eye()
            if unavailable is not None or self._eye is None:
                return Look("unavailable", unavailable or "no-helper")
            if self._state.blind is not None:  # changed while the helper was starting
                return Look("blind", self._state.blind.value)
            try:
                if self._latched:
                    await self._eye.set_blind(False)
                    self._latched = False
                result = await self._eye.capture_window(window.uuid, popups=popups, force=force)
                break
            except CaptureClosedError:
                self._drop_eye()
                if attempt:
                    return Look("unavailable", "helper-exited")
            except CaptureClientError as exc:
                self._drop_eye()
                return Look("error", type(exc).__name__)
        assert result is not None

        now = self._state
        if now.blind is not None or now.window is None or now.window.uuid != window.uuid:
            # The answer describes a moment that is no longer allowed or no longer current.
            self._drop_frame()
            return Look("blind", now.blind.value if now.blind else "stale")
        if isinstance(result, Frame):
            self._frame_uuid = window.uuid
            self._on_frame(result)
            return Look(
                "ok",
                width=result.width,
                height=result.height,
                elapsed_ms=result.elapsed_ms,
                popups=result.popups,
            )
        if isinstance(result, Unchanged):
            return Look("unchanged")
        self._drop_frame()
        if isinstance(result, Blind):
            return Look("blind", f"helper-{result.reason}")
        assert isinstance(result, CaptureError)
        return Look("error", result.code)

    async def _ensure_eye(self) -> str | None:
        """Start the helper if needed. Returns why it is unavailable, or None when ready."""
        if self._eye is not None:
            return None
        try:
            eye = await self._eye_factory()
        except HelperUnavailableError as exc:
            return f"helper-{exc}"
        try:
            hello = await eye.hello()
        except CaptureClientError as exc:
            eye.close()
            return type(exc).__name__
        self._eye = eye
        self._latched = hello.latched
        self._state = replace(self._state, helper=hello)
        log.event(
            "helper.hello",
            protocol=hello.protocol,
            kwin=hello.kwin,
            regime=hello.permission_regime,
            lock_check=hello.lock_check,
            config_ok=hello.config_ok,
        )
        return None

    def _spawn(self, coro: Coroutine[object, object, object]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("senses.task_failed", error=type(task.exception()).__name__)


async def open_helper_eye(binary: Path) -> Eye:
    return HelperEye(start_helper(binary))
