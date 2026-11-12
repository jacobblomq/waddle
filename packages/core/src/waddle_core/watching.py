"""The watching policy: when Waddle may take a look at all (VISION "watching levels").

``CaptureScheduler`` answers one question: given why a capture is wanted, may it happen now?
It is pure and takes the time as a parameter. It only limits *frequency and occasion*; the
privacy gate (``waddle_core.privacy.gate``) is a separate, mandatory check. Callers should
pass the privacy gate first, because a granted request uses up its rate-limit slot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from waddle_core.config import WatchingConfig

MAX_GUIDE_FPS = 1.0
GLANCE_INTERVAL_MS = 10 * 60_000  # one glance per window per ten minutes
AMBIENT_INTERVAL_MS = 30_000
_MAX_UUID_CHARS = 64
_MAX_TRACKED_WINDOWS = 256


class Level(StrEnum):
    ON_DEMAND = "on-demand"  # the user asked; always available
    GUIDE = "guide"  # verifying steps while a guide runs
    GLANCE = "glance"  # one look on app switch to identify an unknown version
    AMBIENT = "ambient"  # occasional looks for proactive tips; opt-in


class Trigger(StrEnum):
    USER_ASK = "user-ask"
    PUSH_TO_TALK = "push-to-talk"
    REGION_SELECT = "region-select"
    SHOW_ME_WHERE = "show-me-where"
    GUIDE_EVENT = "guide-event"
    GUIDE_BACKSTOP = "guide-backstop"
    APP_SWITCH = "app-switch"
    AMBIENT_TICK = "ambient-tick"


LEVEL_OF: dict[Trigger, Level] = {
    Trigger.USER_ASK: Level.ON_DEMAND,
    Trigger.PUSH_TO_TALK: Level.ON_DEMAND,
    Trigger.REGION_SELECT: Level.ON_DEMAND,
    Trigger.SHOW_ME_WHERE: Level.ON_DEMAND,
    Trigger.GUIDE_EVENT: Level.GUIDE,
    Trigger.GUIDE_BACKSTOP: Level.GUIDE,
    Trigger.APP_SWITCH: Level.GLANCE,
    Trigger.AMBIENT_TICK: Level.AMBIENT,
}


@dataclass(frozen=True)
class WatchPolicy:
    glance: bool
    ambient: bool
    guide_max_fps: float

    def __post_init__(self) -> None:
        fps = self.guide_max_fps
        if not (isinstance(fps, int | float) and math.isfinite(fps) and 0 < fps <= MAX_GUIDE_FPS):
            raise ValueError(f"guide_max_fps must be in (0, {MAX_GUIDE_FPS}]")

    @classmethod
    def from_config(cls, config: WatchingConfig) -> WatchPolicy:
        return cls(glance=config.glance, ambient=config.ambient, guide_max_fps=config.guide_max_fps)

    @property
    def guide_min_interval_ms(self) -> int:
        """Shortest gap between two guide captures, rounded up so the cap is never exceeded."""
        return math.ceil(1000 / self.guide_max_fps)

    @property
    def levels(self) -> frozenset[Level]:
        """The watching levels this policy permits."""
        enabled = {Level.ON_DEMAND, Level.GUIDE}
        if self.glance:
            enabled.add(Level.GLANCE)
        if self.ambient:
            enabled.add(Level.AMBIENT)
        return frozenset(enabled)


class CaptureScheduler:
    """Grants or refuses captures by trigger. A granted request counts as a capture."""

    def __init__(self, policy: WatchPolicy) -> None:
        self._policy = policy
        self._last_guide_ms: int | None = None
        self._last_ambient_ms: int | None = None
        self._glanced: dict[str, int] = {}

    @property
    def policy(self) -> WatchPolicy:
        return self._policy

    def set_policy(self, policy: WatchPolicy) -> None:
        """Apply changed settings. Rate-limit history is kept, so a toggle is not a reset."""
        self._policy = policy

    def request(
        self,
        trigger: Trigger,
        now_ms: int,
        *,
        guide_active: bool,
        version_known: bool,
        window_uuid: str | None = None,
    ) -> bool:
        """May a capture for ``trigger`` happen at ``now_ms``? True also records it.

        - On-demand triggers: always.
        - ``GUIDE_EVENT`` / ``GUIDE_BACKSTOP``: only while a guide is active, and together at
          most ``guide_max_fps`` per second.
        - ``APP_SWITCH``: only with glance enabled, only when the app's version is not yet
          known, and at most once per ``window_uuid`` per ten minutes. Without a uuid: never.
        - ``AMBIENT_TICK``: only with ambient enabled, at most once per thirty seconds.
        """
        level = LEVEL_OF.get(trigger)
        if level is Level.ON_DEMAND:
            return True
        if level is Level.GUIDE:
            if not guide_active:
                return False
            if not _elapsed(self._last_guide_ms, now_ms, self._policy.guide_min_interval_ms):
                self._last_guide_ms = _clamp(self._last_guide_ms, now_ms)
                return False
            self._last_guide_ms = now_ms
            return True
        if level is Level.GLANCE:
            return self._request_glance(now_ms, version_known, window_uuid)
        if level is Level.AMBIENT:
            if not self._policy.ambient:
                return False
            if not _elapsed(self._last_ambient_ms, now_ms, AMBIENT_INTERVAL_MS):
                self._last_ambient_ms = _clamp(self._last_ambient_ms, now_ms)
                return False
            self._last_ambient_ms = now_ms
            return True
        return False  # an unknown trigger is refused

    def _request_glance(self, now_ms: int, version_known: bool, window_uuid: str | None) -> bool:
        if not self._policy.glance or version_known:
            return False
        if not isinstance(window_uuid, str) or not 0 < len(window_uuid) <= _MAX_UUID_CHARS:
            return False
        # Forget glances that no longer limit anything; pull future-dated ones back to now.
        self._glanced = {
            uuid: min(at, now_ms)
            for uuid, at in self._glanced.items()
            if now_ms - at < GLANCE_INTERVAL_MS
        }
        if window_uuid in self._glanced:
            return False
        if len(self._glanced) >= _MAX_TRACKED_WINDOWS:
            return False  # more windows than we track: refuse rather than forget a limit
        self._glanced[window_uuid] = now_ms
        return True

    def next_allowed_ms(self, now_ms: int) -> int:
        """Earliest time at or after ``now_ms`` a guide capture would pass the rate limit.

        This is when to schedule the backstop; it says nothing about ``guide_active``.
        """
        if self._last_guide_ms is None:
            return now_ms
        last = min(self._last_guide_ms, now_ms)
        return max(now_ms, last + self._policy.guide_min_interval_ms)

    def reset_guide(self) -> None:
        """Forget the guide limiter (a guide ended). The next guide starts without a wait."""
        self._last_guide_ms = None


def _elapsed(last_ms: int | None, now_ms: int, interval_ms: int) -> bool:
    return last_ms is None or now_ms - last_ms >= interval_ms


def _clamp(last_ms: int | None, now_ms: int) -> int | None:
    """If the clock went backwards, count the interval from now instead of waiting it out."""
    return last_ms if last_ms is None else min(last_ms, now_ms)
