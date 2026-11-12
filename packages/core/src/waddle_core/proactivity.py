"""The proactivity gate: the "don't be Clippy" rules (VISION §9, ARCHITECTURE §4.4).

Every bubble Waddle wants to show goes through ``decide``. Answers to the user's own
requests always pass; alerts are queued rather than lost; tips are rare, never interrupt,
and get rarer when dismissed.

Nothing here reads the clock: every time is an integer millisecond value passed in by the
caller. Any consistent clock works. If it jumps backwards (a monotonic clock after a reboot,
with persisted state), stored times that now lie in the future are treated as "just now",
which errs towards fewer tips and heals itself within the window.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

DEFAULT_TIPS_PER_HOUR = 3
DEFAULT_ALERT_COOLDOWN_MS = 10 * 60_000

MAX_CATEGORY_CHARS = 64
MAX_CATEGORIES = 256
_MAX_TIME_MS = 2**62
_STATE_VERSION = 1


class Kind(StrEnum):
    ANSWER = "answer"  # the user asked
    ALERT = "alert"  # something needs attention (a failed unit, a full disk)
    TIP = "tip"  # unsolicited advice


class Decision(StrEnum):
    SHOW = "show"
    SHOW_SILENT = "show-silent"  # text only, no speech
    DEFER = "defer"  # not now; the caller keeps it and submits it again later
    DROP = "drop"


@dataclass(frozen=True)
class Context:
    dnd: bool  # Plasma's Do Not Disturb
    quiet: bool  # Waddle's quiet mode: no tips, no speech
    focus_mode: bool  # Waddle's Focus mode
    user_idle_ms: int  # time since the last keyboard or pointer activity
    fullscreen: bool  # the focused app is fullscreen
    locked: bool
    paused: bool


def _valid_category(category: object) -> bool:
    return isinstance(category, str) and 0 < len(category) <= MAX_CATEGORY_CHARS


def _valid_time(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_TIME_MS


class TipBudget:
    """How many tips may still be shown, and which categories the user has muted.

    - At most ``per_hour`` tips in any sliding one-hour window.
    - Each dismissal lowers the effective hourly budget by one, down to zero. The penalty
      lasts until an accepted tip cancels it or it is 24 hours old. At most ``per_hour``
      dismissals are remembered (the newest), so one accepted tip always buys one back.
    - An accepted (used) tip cancels the oldest remembered dismissal.
    - "Don't show tips like this" mutes a category until it is unmuted.

    State is small and bounded; ``to_dict``/``from_dict`` give plain JSON data.
    """

    def __init__(self, per_hour: int = DEFAULT_TIPS_PER_HOUR) -> None:
        if isinstance(per_hour, bool) or not isinstance(per_hour, int) or per_hour < 0:
            raise ValueError("per_hour must be a non-negative integer")
        self._per_hour = per_hour
        self._shown: list[int] = []
        self._dismissed: list[int] = []
        self._muted: set[str] = set()

    @property
    def per_hour(self) -> int:
        return self._per_hour

    @property
    def muted(self) -> frozenset[str]:
        return frozenset(self._muted)

    def _settle(self, now_ms: int) -> None:
        """Forget what has expired; pull future-dated entries back to now."""
        self._shown = sorted(min(t, now_ms) for t in self._shown if now_ms - t < HOUR_MS)
        if self._per_hour:
            self._shown = self._shown[-self._per_hour :]
            kept = sorted(min(t, now_ms) for t in self._dismissed if now_ms - t < DAY_MS)
            self._dismissed = kept[-self._per_hour :]
        else:
            self._shown = []
            self._dismissed = []

    def effective_budget(self, now_ms: int) -> int:
        """The hourly budget after the dismissal penalty."""
        self._settle(now_ms)
        return max(0, self._per_hour - len(self._dismissed))

    def remaining(self, now_ms: int) -> int:
        """Tips that may still be shown right now."""
        return max(0, self.effective_budget(now_ms) - len(self._shown))

    def is_muted(self, category: str) -> bool:
        return category in self._muted

    def can_show(self, category: str, now_ms: int) -> bool:
        return (
            _valid_category(category) and not self.is_muted(category) and self.remaining(now_ms) > 0
        )

    def record_shown(self, category: str, now_ms: int) -> None:
        """A tip was shown. ``category`` is accepted for symmetry; the budget is global."""
        self._settle(now_ms)
        self._shown.append(now_ms)

    def record_dismissed(self, category: str, now_ms: int) -> None:
        """The user closed a tip without using it."""
        self._dismissed.append(now_ms)
        self._settle(now_ms)

    def record_accepted(self, category: str, now_ms: int) -> None:
        """The user used a tip: give one back, never above the default."""
        self._settle(now_ms)
        if self._dismissed:
            self._dismissed.pop(0)

    def mute(self, category: str) -> None:
        """ "Don't show tips like this." """
        if not _valid_category(category):
            raise ValueError("category must be a non-empty string of at most 64 characters")
        if len(self._muted) >= MAX_CATEGORIES and category not in self._muted:
            raise ValueError("too many muted categories")
        self._muted.add(category)

    def unmute(self, category: str) -> None:
        self._muted.discard(category)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _STATE_VERSION,
            "shown_ms": sorted(self._shown),
            "dismissed_ms": sorted(self._dismissed),
            "muted": sorted(self._muted),
        }

    @classmethod
    def from_dict(cls, data: object, per_hour: int = DEFAULT_TIPS_PER_HOUR) -> TipBudget:
        """Restore saved state. Anything malformed is ignored, entry by entry.

        A state file is Waddle's own, but it may be truncated, hand-edited or from another
        version; none of that may stop Waddle from starting. Entries beyond what a budget
        can ever hold are discarded (the newest are kept).
        """
        budget = cls(per_hour)
        if not isinstance(data, dict):
            return budget

        def times(key: str) -> list[int]:
            raw = data.get(key)
            if not isinstance(raw, list):
                return []
            good = sorted(t for t in raw if _valid_time(t))
            return good[-per_hour:] if per_hour else []

        budget._shown = times("shown_ms")
        budget._dismissed = times("dismissed_ms")
        muted = data.get("muted")
        if isinstance(muted, list):
            budget._muted = set(sorted(c for c in muted if _valid_category(c))[:MAX_CATEGORIES])
        return budget


class AlertCooldown:
    """Per-category memory of when an alert was last shown, so it is not repeated.

    ``ready`` asks; ``record_shown`` is called once the alert is actually on screen. Kept in
    memory only.
    """

    def __init__(self, default_cooldown_ms: int = DEFAULT_ALERT_COOLDOWN_MS) -> None:
        if default_cooldown_ms < 0:
            raise ValueError("default_cooldown_ms must not be negative")
        self._default = default_cooldown_ms
        self._cooldowns: dict[str, int] = {}
        self._last: dict[str, int] = {}

    def set_cooldown(self, category: str, cooldown_ms: int) -> None:
        if not _valid_category(category):
            raise ValueError("category must be a non-empty string of at most 64 characters")
        if cooldown_ms < 0:
            raise ValueError("cooldown_ms must not be negative")
        if len(self._cooldowns) >= MAX_CATEGORIES and category not in self._cooldowns:
            raise ValueError("too many alert categories")
        self._cooldowns[category] = cooldown_ms

    def cooldown_ms(self, category: str) -> int:
        return self._cooldowns.get(category, self._default)

    def ready(self, category: str, now_ms: int) -> bool:
        """True when ``category`` was never shown or its cooldown has fully elapsed."""
        if not _valid_category(category):
            return False
        last = self._last.get(category)
        if last is None:
            return True
        if now_ms < last:  # the clock went backwards: count the cooldown from now
            self._last[category] = now_ms
            return False
        return now_ms - last >= self.cooldown_ms(category)

    def record_shown(self, category: str, now_ms: int) -> None:
        if not _valid_category(category):
            return
        self._last.pop(category, None)
        if len(self._last) >= MAX_CATEGORIES:  # forget the alert shown longest ago
            del self._last[min(self._last, key=self._last.__getitem__)]
        self._last[category] = now_ms

    def next_ready_ms(self, category: str) -> int:
        """Earliest time ``ready`` turns True; 0 if the category was never shown."""
        last = self._last.get(category)
        return 0 if last is None else last + self.cooldown_ms(category)


def decide(
    kind: Kind,
    category: str,
    ctx: Context,
    budget: TipBudget,
    now_ms: int,
    *,
    min_idle_ms: int = 1500,
    confidence: float = 1.0,
    min_confidence: float = 0.8,
) -> Decision:
    """Whether a bubble may be shown now. Pure: records nothing.

    - ANSWER: shown always, silently in quiet mode; dropped only when locked.
    - ALERT: dropped when locked; deferred under Do Not Disturb or Focus mode (the caller
      queues it and submits it again); silent in quiet mode or over a fullscreen app.
      Repetition is the caller's check, with ``AlertCooldown``.
    - TIP: dropped when locked, paused, Do Not Disturb, quiet, Focus mode, fullscreen, the
      category is muted, confidence is below ``min_confidence``, or the budget is spent;
      deferred while the user has been idle for less than ``min_idle_ms`` (never interrupt
      mid-action). On SHOW the caller calls ``budget.record_shown(category, now_ms)``.
    """
    if ctx.locked:
        return Decision.DROP
    if kind is Kind.ANSWER:
        return Decision.SHOW_SILENT if ctx.quiet else Decision.SHOW
    if kind is Kind.ALERT:
        if ctx.dnd or ctx.focus_mode:
            return Decision.DEFER
        if ctx.quiet or ctx.fullscreen:
            return Decision.SHOW_SILENT
        return Decision.SHOW
    # Everything else is held to the tip rules, the strictest set.
    if ctx.paused or ctx.dnd or ctx.quiet or ctx.focus_mode or ctx.fullscreen:
        return Decision.DROP
    if not (confidence >= min_confidence):  # also drops NaN
        return Decision.DROP
    if not budget.can_show(category, now_ms):
        return Decision.DROP
    if ctx.user_idle_ms < min_idle_ms:
        return Decision.DEFER
    return Decision.SHOW
