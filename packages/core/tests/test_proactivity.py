import itertools
import json

import pytest

from waddle_core.proactivity import (
    DAY_MS,
    DEFAULT_ALERT_COOLDOWN_MS,
    HOUR_MS,
    MAX_CATEGORIES,
    AlertCooldown,
    Context,
    Decision,
    Kind,
    TipBudget,
    decide,
)

T0 = 1_000_000_000
MIN = 60_000
FLAGS = ("dnd", "quiet", "focus_mode", "fullscreen", "locked", "paused")


def ctx(user_idle_ms: int = 10_000, **flags: bool) -> Context:
    values = dict.fromkeys(FLAGS, False) | flags
    return Context(user_idle_ms=user_idle_ms, **values)


def all_contexts(user_idle_ms: int = 10_000) -> list[Context]:
    return [
        ctx(user_idle_ms, **dict(zip(FLAGS, bits, strict=True)))
        for bits in itertools.product([False, True], repeat=len(FLAGS))
    ]


# --- decide: truth tables ------------------------------------------------------------------


def test_answer_truth_table() -> None:
    spent = TipBudget(per_hour=0)
    for c, idle in itertools.product(all_contexts(), (0, 10_000)):
        c = Context(**{**vars(c), "user_idle_ms": idle})
        got = decide(Kind.ANSWER, "anything", c, spent, T0, confidence=0.0)
        if c.locked:
            assert got is Decision.DROP, c
        elif c.quiet:
            assert got is Decision.SHOW_SILENT, c
        else:
            assert got is Decision.SHOW, c


def test_alert_truth_table() -> None:
    spent = TipBudget(per_hour=0)
    for c in all_contexts(user_idle_ms=0):
        got = decide(Kind.ALERT, "disk-full", c, spent, T0, confidence=0.0)
        if c.locked:
            assert got is Decision.DROP, c
        elif c.dnd or c.focus_mode:
            assert got is Decision.DEFER, c
        elif c.quiet or c.fullscreen:
            assert got is Decision.SHOW_SILENT, c
        else:
            assert got is Decision.SHOW, c


def test_alert_is_never_dropped_unless_locked() -> None:
    results = {decide(Kind.ALERT, "x", c, TipBudget(), T0) for c in all_contexts() if not c.locked}
    assert results == {Decision.SHOW, Decision.SHOW_SILENT, Decision.DEFER}


@pytest.mark.parametrize("idle_ms", [0, 1499, 1500, 60_000])
def test_tip_truth_table(idle_ms: int) -> None:
    shown = 0
    for c in all_contexts(idle_ms):
        got = decide(Kind.TIP, "shortcut", c, TipBudget(), T0)
        if any(getattr(c, flag) for flag in FLAGS):
            assert got is Decision.DROP, c
        elif idle_ms < 1500:
            assert got is Decision.DEFER, c
        else:
            assert got is Decision.SHOW, c
            shown += 1
    assert shown == (1 if idle_ms >= 1500 else 0)


def test_tip_is_never_silent() -> None:
    for c in all_contexts():
        assert decide(Kind.TIP, "shortcut", c, TipBudget(), T0) is not Decision.SHOW_SILENT


def test_tip_idle_threshold_is_configurable_and_inclusive() -> None:
    budget = TipBudget()
    assert decide(Kind.TIP, "c", ctx(2999), budget, T0, min_idle_ms=3000) is Decision.DEFER
    assert decide(Kind.TIP, "c", ctx(3000), budget, T0, min_idle_ms=3000) is Decision.SHOW
    assert decide(Kind.TIP, "c", ctx(0), budget, T0, min_idle_ms=0) is Decision.SHOW


@pytest.mark.parametrize(
    ("confidence", "min_confidence", "expected"),
    [
        (0.8, 0.8, Decision.SHOW),
        (0.79, 0.8, Decision.DROP),
        (1.0, 0.8, Decision.SHOW),
        (0.5, 0.4, Decision.SHOW),
        (0.0, 0.8, Decision.DROP),
        (float("nan"), 0.8, Decision.DROP),
        (0.9, float("nan"), Decision.DROP),
    ],
)
def test_tip_confidence(confidence: float, min_confidence: float, expected: Decision) -> None:
    got = decide(
        Kind.TIP, "c", ctx(), TipBudget(), T0, confidence=confidence, min_confidence=min_confidence
    )
    assert got is expected


def test_tip_muted_category_is_dropped_others_are_not() -> None:
    budget = TipBudget()
    budget.mute("shortcuts")
    assert decide(Kind.TIP, "shortcuts", ctx(), budget, T0) is Decision.DROP
    assert decide(Kind.TIP, "layout", ctx(), budget, T0) is Decision.SHOW
    # Muting tips does not silence answers or alerts that happen to share a name.
    assert decide(Kind.ANSWER, "shortcuts", ctx(), budget, T0) is Decision.SHOW
    assert decide(Kind.ALERT, "shortcuts", ctx(), budget, T0) is Decision.SHOW
    budget.unmute("shortcuts")
    assert decide(Kind.TIP, "shortcuts", ctx(), budget, T0) is Decision.SHOW


def test_tip_drop_beats_defer() -> None:
    """A tip that will be dropped anyway must not be queued by a busy user."""
    budget = TipBudget()
    budget.mute("m")
    assert decide(Kind.TIP, "m", ctx(0), budget, T0) is Decision.DROP
    assert decide(Kind.TIP, "c", ctx(0), budget, T0, confidence=0.1) is Decision.DROP
    assert decide(Kind.TIP, "c", ctx(0), TipBudget(per_hour=0), T0) is Decision.DROP
    assert decide(Kind.TIP, "c", ctx(0, quiet=True), budget, T0) is Decision.DROP


def test_tip_budget_exhaustion_through_decide() -> None:
    budget = TipBudget()
    for i in range(3):
        now = T0 + i * MIN
        assert decide(Kind.TIP, "c", ctx(), budget, now) is Decision.SHOW
        budget.record_shown("c", now)
    assert decide(Kind.TIP, "c", ctx(), budget, T0 + 3 * MIN) is Decision.DROP
    assert decide(Kind.ANSWER, "c", ctx(), budget, T0 + 3 * MIN) is Decision.SHOW
    assert decide(Kind.ALERT, "c", ctx(), budget, T0 + 3 * MIN) is Decision.SHOW


def test_decide_does_not_record_anything() -> None:
    budget = TipBudget()
    before = budget.to_dict()
    for _ in range(10):
        assert decide(Kind.TIP, "c", ctx(), budget, T0) is Decision.SHOW
    assert budget.to_dict() == before and budget.remaining(T0) == 3


def test_tip_with_invalid_category_is_dropped() -> None:
    assert decide(Kind.TIP, "", ctx(), TipBudget(), T0) is Decision.DROP
    assert decide(Kind.TIP, "x" * 65, ctx(), TipBudget(), T0) is Decision.DROP
    assert decide(Kind.TIP, "x" * 64, ctx(), TipBudget(), T0) is Decision.SHOW


# --- TipBudget arithmetic --------------------------------------------------------------------


def test_default_budget_is_three_per_hour() -> None:
    budget = TipBudget()
    assert budget.per_hour == 3 and budget.remaining(T0) == 3 and budget.effective_budget(T0) == 3


def test_window_slides_one_tip_at_a_time() -> None:
    budget = TipBudget()
    for offset in (0, 10 * MIN, 20 * MIN):
        budget.record_shown("c", T0 + offset)
    assert budget.remaining(T0 + 20 * MIN) == 0
    assert budget.remaining(T0 + HOUR_MS - 1) == 0, "the first tip is still inside the window"
    assert budget.remaining(T0 + HOUR_MS) == 1, "exactly one hour later it has left"
    assert budget.remaining(T0 + HOUR_MS + 10 * MIN - 1) == 1
    assert budget.remaining(T0 + HOUR_MS + 10 * MIN) == 2
    assert budget.remaining(T0 + HOUR_MS + 20 * MIN) == 3


def test_each_dismissal_lowers_the_budget_down_to_zero() -> None:
    budget = TipBudget()
    for n, expected in enumerate([2, 1, 0, 0, 0], start=1):
        budget.record_dismissed("c", T0 + n)
        assert budget.effective_budget(T0 + n) == expected
    assert budget.remaining(T0 + 10) == 0
    assert not budget.can_show("c", T0 + 10)


def test_dismissal_penalty_combines_with_tips_already_shown() -> None:
    budget = TipBudget()
    budget.record_shown("c", T0)
    assert budget.remaining(T0) == 2
    budget.record_dismissed("c", T0 + 1)
    assert budget.remaining(T0 + 1) == 1
    budget.record_shown("c", T0 + 2)
    assert budget.remaining(T0 + 2) == 0
    # An hour on, the shown tips have left the window; the dismissal still counts.
    assert budget.remaining(T0 + 2 + HOUR_MS) == 2


def test_accepted_tip_restores_one_but_never_above_default() -> None:
    budget = TipBudget()
    budget.record_dismissed("c", T0)
    budget.record_dismissed("c", T0 + 1)
    assert budget.effective_budget(T0 + 2) == 1
    budget.record_accepted("c", T0 + 2)
    assert budget.effective_budget(T0 + 2) == 2
    budget.record_accepted("c", T0 + 3)
    assert budget.effective_budget(T0 + 3) == 3
    for i in range(5):
        budget.record_accepted("c", T0 + 4 + i)
    assert budget.effective_budget(T0 + 10) == 3
    assert budget.remaining(T0 + 10) == 3


def test_one_accept_always_buys_one_back_even_after_many_dismissals() -> None:
    budget = TipBudget()
    for i in range(20):
        budget.record_dismissed("c", T0 + i)
    assert budget.effective_budget(T0 + 20) == 0
    budget.record_accepted("c", T0 + 21)
    assert budget.effective_budget(T0 + 21) == 1


def test_accept_cancels_the_oldest_dismissal() -> None:
    budget = TipBudget()
    budget.record_dismissed("c", T0)
    budget.record_dismissed("c", T0 + 5 * HOUR_MS)
    budget.record_accepted("c", T0 + 5 * HOUR_MS + 1)
    # Had the newer one been cancelled, the budget would be whole again at T0 + 24 h.
    assert budget.effective_budget(T0 + DAY_MS) == 2
    assert budget.effective_budget(T0 + 5 * HOUR_MS + DAY_MS) == 3


def test_dismissals_are_forgotten_after_24_hours() -> None:
    budget = TipBudget()
    budget.record_dismissed("c", T0)
    budget.record_dismissed("c", T0 + HOUR_MS)
    assert budget.effective_budget(T0 + DAY_MS - 1) == 1
    assert budget.effective_budget(T0 + DAY_MS) == 2
    assert budget.effective_budget(T0 + DAY_MS + HOUR_MS - 1) == 2
    assert budget.effective_budget(T0 + DAY_MS + HOUR_MS) == 3


def test_custom_and_zero_budgets() -> None:
    assert TipBudget(per_hour=1).remaining(T0) == 1
    zero = TipBudget(per_hour=0)
    zero.record_accepted("c", T0)
    assert zero.remaining(T0) == 0 and not zero.can_show("c", T0)
    for bad in (-1, True, 2.5, "3"):
        with pytest.raises(ValueError):
            TipBudget(per_hour=bad)  # type: ignore[arg-type]


def test_recording_more_than_the_budget_does_not_go_negative_or_grow() -> None:
    budget = TipBudget()
    for i in range(50):
        budget.record_shown("c", T0 + i)
        budget.record_dismissed("c", T0 + i)
    assert budget.remaining(T0 + 50) == 0
    state = budget.to_dict()
    assert len(state["shown_ms"]) <= 3 and len(state["dismissed_ms"]) <= 3


def test_clock_going_backwards_errs_towards_fewer_tips_and_recovers() -> None:
    budget = TipBudget()
    for i in range(3):
        budget.record_shown("c", T0 + i)
    earlier = T0 - 5 * HOUR_MS
    assert budget.remaining(earlier) == 0, "future-dated tips still count"
    assert budget.remaining(earlier + HOUR_MS - 1) == 0
    assert budget.remaining(earlier + HOUR_MS) == 3, "and expire an hour after the jump"


def test_mute_and_unmute() -> None:
    budget = TipBudget()
    budget.mute("shortcuts")
    budget.mute("shortcuts")
    assert budget.is_muted("shortcuts") and not budget.is_muted("layout")
    assert budget.muted == frozenset({"shortcuts"})
    assert not budget.can_show("shortcuts", T0) and budget.can_show("layout", T0)
    budget.unmute("shortcuts")
    budget.unmute("never-muted")
    assert budget.muted == frozenset()
    for bad in ("", "x" * 65, 7, None):
        with pytest.raises(ValueError):
            budget.mute(bad)  # type: ignore[arg-type]


def test_mute_is_bounded() -> None:
    budget = TipBudget()
    for i in range(MAX_CATEGORIES):
        budget.mute(f"c{i}")
    budget.mute("c0")  # already muted: fine
    with pytest.raises(ValueError):
        budget.mute("one-too-many")


def test_serialization_round_trip_preserves_behaviour() -> None:
    budget = TipBudget()
    budget.record_shown("a", T0)
    budget.record_shown("b", T0 + MIN)
    budget.record_dismissed("b", T0 + 2 * MIN)
    budget.mute("b")
    budget.mute("zeta")
    state = budget.to_dict()
    restored = TipBudget.from_dict(json.loads(json.dumps(state)))
    assert restored.to_dict() == state
    assert state["muted"] == ["b", "zeta"]
    assert all(type(t) is int for t in state["shown_ms"] + state["dismissed_ms"])
    for now in (T0 + 3 * MIN, T0 + HOUR_MS, T0 + HOUR_MS + MIN, T0 + DAY_MS + 2 * MIN):
        assert restored.remaining(now) == budget.remaining(now)
    assert restored.is_muted("b") and restored.is_muted("zeta") and not restored.is_muted("a")
    assert TipBudget.from_dict(TipBudget().to_dict()).to_dict() == TipBudget().to_dict()


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        [],
        "state",
        42,
        {},
        {"shown_ms": "soon", "dismissed_ms": 7, "muted": {"a": 1}},
        {"shown_ms": [None, "1", 1.5, True, -5, [1], {}], "dismissed_ms": [2**80], "muted": [1]},
        {"muted": ["", "x" * 65, None, ["a"]]},
        {"version": "nine", "extra": object()},
    ],
)
def test_from_dict_tolerates_garbage(garbage: object) -> None:
    budget = TipBudget.from_dict(garbage)
    assert budget.remaining(T0) == 3
    assert budget.muted == frozenset()
    assert budget.to_dict() == TipBudget().to_dict()


def test_from_dict_keeps_the_good_entries_among_bad_ones() -> None:
    data = {
        "shown_ms": [T0, "bad", T0 + 1],
        "dismissed_ms": [None, T0 + 2],
        "muted": ["ok", 5, ""],
    }
    budget = TipBudget.from_dict(data)
    assert budget.to_dict() == {
        "version": 1,
        "shown_ms": [T0, T0 + 1],
        "dismissed_ms": [T0 + 2],
        "muted": ["ok"],
    }
    assert budget.remaining(T0 + 3) == 0  # 3 - 1 dismissal - 2 shown


def test_from_dict_bounds_oversized_state() -> None:
    data = {
        "shown_ms": list(range(T0, T0 + 10_000)),
        "dismissed_ms": list(range(T0, T0 + 10_000)),
        "muted": [f"c{i:05d}" for i in range(10_000)],
    }
    state = TipBudget.from_dict(data).to_dict()
    assert state["shown_ms"] == [T0 + 9997, T0 + 9998, T0 + 9999], "the newest are kept"
    assert len(state["dismissed_ms"]) == 3
    assert len(state["muted"]) == MAX_CATEGORIES


def test_from_dict_respects_a_custom_budget() -> None:
    budget = TipBudget.from_dict({"shown_ms": [T0]}, per_hour=1)
    assert budget.per_hour == 1 and budget.remaining(T0 + 1) == 0
    assert budget.remaining(T0 + HOUR_MS) == 1


# --- AlertCooldown ---------------------------------------------------------------------------


def test_alert_cooldown_blocks_repeats_until_the_interval_has_passed() -> None:
    cooldown = AlertCooldown(default_cooldown_ms=5 * MIN)
    assert cooldown.ready("disk-full", T0)
    assert cooldown.next_ready_ms("disk-full") == 0
    cooldown.record_shown("disk-full", T0)
    assert not cooldown.ready("disk-full", T0)
    assert not cooldown.ready("disk-full", T0 + 5 * MIN - 1)
    assert cooldown.ready("disk-full", T0 + 5 * MIN)
    assert cooldown.next_ready_ms("disk-full") == T0 + 5 * MIN
    assert cooldown.ready("unit-failed", T0 + 1), "categories are independent"


def test_alert_cooldown_ready_does_not_record() -> None:
    cooldown = AlertCooldown()
    for _ in range(3):
        assert cooldown.ready("a", T0)
    cooldown.record_shown("a", T0)
    assert not cooldown.ready("a", T0 + DEFAULT_ALERT_COOLDOWN_MS - 1)
    assert cooldown.ready("a", T0 + DEFAULT_ALERT_COOLDOWN_MS)


def test_alert_cooldown_per_category_override() -> None:
    cooldown = AlertCooldown(default_cooldown_ms=5 * MIN)
    cooldown.set_cooldown("battery", 30 * MIN)
    cooldown.set_cooldown("always", 0)
    assert cooldown.cooldown_ms("battery") == 30 * MIN and cooldown.cooldown_ms("other") == 5 * MIN
    for category in ("battery", "other", "always"):
        cooldown.record_shown(category, T0)
    assert cooldown.ready("always", T0)
    assert cooldown.ready("other", T0 + 5 * MIN)
    assert not cooldown.ready("battery", T0 + 30 * MIN - 1)
    assert cooldown.ready("battery", T0 + 30 * MIN)


def test_alert_cooldown_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        AlertCooldown(default_cooldown_ms=-1)
    cooldown = AlertCooldown()
    with pytest.raises(ValueError):
        cooldown.set_cooldown("a", -1)
    with pytest.raises(ValueError):
        cooldown.set_cooldown("", 10)
    assert not cooldown.ready("", T0)
    cooldown.record_shown("x" * 65, T0)
    assert cooldown.next_ready_ms("x" * 65) == 0


def test_alert_cooldown_clock_going_backwards_restarts_the_wait() -> None:
    cooldown = AlertCooldown(default_cooldown_ms=5 * MIN)
    cooldown.record_shown("a", T0)
    earlier = T0 - HOUR_MS
    assert not cooldown.ready("a", earlier)
    assert not cooldown.ready("a", earlier + 5 * MIN - 1)
    assert cooldown.ready("a", earlier + 5 * MIN)


def test_alert_cooldown_memory_is_bounded_and_forgets_the_oldest() -> None:
    cooldown = AlertCooldown(default_cooldown_ms=DAY_MS)
    for i in range(MAX_CATEGORIES + 1):
        cooldown.record_shown(f"c{i}", T0 + i)
    assert cooldown.ready("c0", T0 + 1000), "the oldest was forgotten"
    assert not cooldown.ready("c1", T0 + 1000)
    assert not cooldown.ready(f"c{MAX_CATEGORIES}", T0 + 1000)
