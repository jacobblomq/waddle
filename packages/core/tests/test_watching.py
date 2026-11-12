import itertools

import pytest

from waddle_core.config import WatchingConfig
from waddle_core.watching import (
    AMBIENT_INTERVAL_MS,
    GLANCE_INTERVAL_MS,
    LEVEL_OF,
    CaptureScheduler,
    Level,
    Trigger,
    WatchPolicy,
)

T0 = 5_000_000
ON_DEMAND = (
    Trigger.USER_ASK,
    Trigger.PUSH_TO_TALK,
    Trigger.REGION_SELECT,
    Trigger.SHOW_ME_WHERE,
)
GUIDE = (Trigger.GUIDE_EVENT, Trigger.GUIDE_BACKSTOP)
UUID_A = "{aaaaaaaa-0000-0000-0000-000000000001}"
UUID_B = "{bbbbbbbb-0000-0000-0000-000000000002}"


def scheduler(glance: bool = True, ambient: bool = True, fps: float = 1.0) -> CaptureScheduler:
    return CaptureScheduler(WatchPolicy(glance=glance, ambient=ambient, guide_max_fps=fps))


def glance(s: CaptureScheduler, now: int, uuid: str | None = UUID_A, known: bool = False) -> bool:
    return s.request(
        Trigger.APP_SWITCH, now, guide_active=False, version_known=known, window_uuid=uuid
    )


def guide(s: CaptureScheduler, now: int, trigger: Trigger = Trigger.GUIDE_EVENT) -> bool:
    return s.request(trigger, now, guide_active=True, version_known=True)


def ambient(s: CaptureScheduler, now: int) -> bool:
    return s.request(Trigger.AMBIENT_TICK, now, guide_active=False, version_known=True)


# --- policy --------------------------------------------------------------------------------


def test_policy_from_default_config() -> None:
    policy = WatchPolicy.from_config(WatchingConfig())
    assert policy == WatchPolicy(glance=True, ambient=False, guide_max_fps=1.0)
    assert policy.levels == {Level.ON_DEMAND, Level.GUIDE, Level.GLANCE}


def test_policy_from_custom_config() -> None:
    config = WatchingConfig(glance=False, ambient=True, guide_max_fps=0.25)
    policy = WatchPolicy.from_config(config)
    assert policy == WatchPolicy(glance=False, ambient=True, guide_max_fps=0.25)
    assert policy.levels == {Level.ON_DEMAND, Level.GUIDE, Level.AMBIENT}
    assert policy.guide_min_interval_ms == 4000


@pytest.mark.parametrize(("fps", "interval"), [(1.0, 1000), (0.5, 2000), (0.3, 3334), (0.1, 10000)])
def test_guide_interval_rounds_up(fps: float, interval: int) -> None:
    assert WatchPolicy(True, False, fps).guide_min_interval_ms == interval


@pytest.mark.parametrize("fps", [0.0, -1.0, 1.01, 30.0, float("inf"), float("nan")])
def test_policy_rejects_out_of_range_fps(fps: float) -> None:
    with pytest.raises(ValueError):
        WatchPolicy(glance=True, ambient=False, guide_max_fps=fps)


def test_every_trigger_has_a_level() -> None:
    assert set(LEVEL_OF) == set(Trigger)
    assert {t for t, level in LEVEL_OF.items() if level is Level.ON_DEMAND} == set(ON_DEMAND)
    assert {t for t, level in LEVEL_OF.items() if level is Level.GUIDE} == set(GUIDE)


# --- on-demand -----------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ON_DEMAND)
def test_on_demand_is_always_allowed(trigger: Trigger) -> None:
    for g, a, active, known in itertools.product([False, True], repeat=4):
        s = scheduler(glance=g, ambient=a, fps=0.1)
        for _ in range(5):  # no rate limit, even at the same instant
            assert s.request(trigger, T0, guide_active=active, version_known=known)


def test_on_demand_does_not_consume_other_limits() -> None:
    s = scheduler()
    for trigger in ON_DEMAND:
        assert s.request(trigger, T0, guide_active=True, version_known=False, window_uuid=UUID_A)
    assert guide(s, T0) and ambient(s, T0) and glance(s, T0)


# --- guide ---------------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", GUIDE)
def test_guide_triggers_need_an_active_guide(trigger: Trigger) -> None:
    s = scheduler()
    assert not s.request(trigger, T0, guide_active=False, version_known=True)
    # The refusal did not start the limiter.
    assert s.request(trigger, T0, guide_active=True, version_known=True)


@pytest.mark.parametrize(("fps", "interval"), [(1.0, 1000), (0.5, 2000), (0.3, 3334)])
def test_guide_rate_limit_boundaries(fps: float, interval: int) -> None:
    s = scheduler(fps=fps)
    assert guide(s, T0)
    assert not guide(s, T0)
    assert not guide(s, T0 + interval - 1)
    assert guide(s, T0 + interval)
    assert not guide(s, T0 + 2 * interval - 1)
    assert guide(s, T0 + 2 * interval)


def test_guide_limit_is_shared_by_both_guide_triggers() -> None:
    s = scheduler()
    assert guide(s, T0, Trigger.GUIDE_EVENT)
    assert not guide(s, T0 + 500, Trigger.GUIDE_BACKSTOP)
    assert guide(s, T0 + 1000, Trigger.GUIDE_BACKSTOP)
    assert not guide(s, T0 + 1500, Trigger.GUIDE_EVENT)
    assert guide(s, T0 + 2000, Trigger.GUIDE_EVENT)


def test_refused_guide_requests_do_not_push_the_limit_back() -> None:
    s = scheduler()
    assert guide(s, T0)
    for offset in range(100, 1000, 100):
        assert not guide(s, T0 + offset)
    assert guide(s, T0 + 1000)


def test_guide_never_exceeds_the_cap_under_a_flood() -> None:
    s = scheduler(fps=0.5)
    granted = [t for t in range(T0, T0 + 60_000, 50) if guide(s, t)]
    assert len(granted) == 30
    assert all(b - a >= 2000 for a, b in itertools.pairwise(granted))


def test_guide_ignores_version_and_watch_toggles() -> None:
    s = scheduler(glance=False, ambient=False)
    assert s.request(Trigger.GUIDE_EVENT, T0, guide_active=True, version_known=False)


def test_next_allowed_ms() -> None:
    s = scheduler(fps=0.5)
    assert s.next_allowed_ms(T0) == T0, "nothing captured yet"
    assert guide(s, T0)
    assert s.next_allowed_ms(T0) == T0 + 2000
    assert s.next_allowed_ms(T0 + 1999) == T0 + 2000
    assert s.next_allowed_ms(T0 + 2000) == T0 + 2000
    assert s.next_allowed_ms(T0 + 9000) == T0 + 9000, "never in the past"
    # The promise holds: a request at the reported time is granted, one just before is not.
    assert not guide(s, s.next_allowed_ms(T0 + 1) - 1)
    assert guide(s, s.next_allowed_ms(T0 + 1))


def test_reset_guide_clears_the_limiter() -> None:
    s = scheduler()
    assert guide(s, T0)
    assert not guide(s, T0 + 1)
    s.reset_guide()
    assert s.next_allowed_ms(T0 + 1) == T0 + 1
    assert guide(s, T0 + 1)


def test_guide_clock_going_backwards_waits_one_interval_from_now() -> None:
    s = scheduler()
    assert guide(s, T0)
    earlier = T0 - 60_000
    assert not guide(s, earlier)
    assert s.next_allowed_ms(earlier) == earlier + 1000
    assert not guide(s, earlier + 999)
    assert guide(s, earlier + 1000)


# --- glance (app switch) -------------------------------------------------------------------


def test_glance_once_per_window_per_ten_minutes() -> None:
    s = scheduler()
    assert glance(s, T0)
    assert not glance(s, T0)
    assert not glance(s, T0 + GLANCE_INTERVAL_MS - 1)
    assert glance(s, T0 + GLANCE_INTERVAL_MS)
    assert not glance(s, T0 + GLANCE_INTERVAL_MS + 1)


def test_glance_limit_is_per_window() -> None:
    s = scheduler()
    assert glance(s, T0, UUID_A)
    assert glance(s, T0 + 1, UUID_B)
    assert not glance(s, T0 + 2, UUID_A)
    assert not glance(s, T0 + 2, UUID_B)
    assert glance(s, T0 + GLANCE_INTERVAL_MS, UUID_A)
    assert not glance(s, T0 + GLANCE_INTERVAL_MS, UUID_B)
    assert glance(s, T0 + GLANCE_INTERVAL_MS + 1, UUID_B)


def test_glance_disabled_really_blocks() -> None:
    s = scheduler(glance=False)
    for now in (T0, T0 + GLANCE_INTERVAL_MS, T0 + 10 * GLANCE_INTERVAL_MS):
        assert not glance(s, now)


def test_glance_only_when_version_unknown() -> None:
    s = scheduler()
    assert not glance(s, T0, known=True)
    assert glance(s, T0, known=False), "a refused request did not use the slot"


def test_glance_needs_a_plausible_uuid() -> None:
    s = scheduler()
    assert not glance(s, T0, uuid=None)
    assert not glance(s, T0, uuid="")
    assert not glance(s, T0, uuid="x" * 65)
    assert not s.request(Trigger.APP_SWITCH, T0, guide_active=False, version_known=False)
    assert not glance(s, T0, uuid=12345)  # type: ignore[arg-type]
    assert glance(s, T0, uuid="x" * 64)


def test_glance_independent_of_guide_state() -> None:
    s = scheduler()
    assert s.request(
        Trigger.APP_SWITCH, T0, guide_active=True, version_known=False, window_uuid=UUID_A
    )


def test_glance_tracking_is_bounded_and_fails_closed() -> None:
    s = scheduler()
    for i in range(256):
        assert glance(s, T0 + i, uuid=f"w{i}")
    assert not glance(s, T0 + 300, uuid="one-more"), "refuse rather than forget a limit"
    assert not glance(s, T0 + 300, uuid="w0")
    # Once old entries expire there is room again.
    assert glance(s, T0 + GLANCE_INTERVAL_MS, uuid="one-more")
    assert not glance(s, T0 + GLANCE_INTERVAL_MS, uuid="w1"), "w1 is still inside its interval"
    assert glance(s, T0 + GLANCE_INTERVAL_MS + 1, uuid="w0")


def test_glance_clock_going_backwards_waits_from_now() -> None:
    s = scheduler()
    assert glance(s, T0)
    earlier = T0 - 3 * GLANCE_INTERVAL_MS
    assert not glance(s, earlier)
    assert not glance(s, earlier + GLANCE_INTERVAL_MS - 1)
    assert glance(s, earlier + GLANCE_INTERVAL_MS)


# --- ambient -------------------------------------------------------------------------------


def test_ambient_once_per_thirty_seconds() -> None:
    s = scheduler()
    assert AMBIENT_INTERVAL_MS == 30_000
    assert ambient(s, T0)
    assert not ambient(s, T0 + 1)
    assert not ambient(s, T0 + AMBIENT_INTERVAL_MS - 1)
    assert ambient(s, T0 + AMBIENT_INTERVAL_MS)
    assert not ambient(s, T0 + 2 * AMBIENT_INTERVAL_MS - 1)


def test_ambient_disabled_really_blocks() -> None:
    s = scheduler(ambient=False)
    for now in (T0, T0 + AMBIENT_INTERVAL_MS, T0 + 100 * AMBIENT_INTERVAL_MS):
        assert not ambient(s, now)


def test_default_config_never_grants_ambient() -> None:
    s = CaptureScheduler(WatchPolicy.from_config(WatchingConfig()))
    assert not ambient(s, T0)
    assert glance(s, T0)


def test_ambient_and_guide_limits_are_independent() -> None:
    s = scheduler()
    assert ambient(s, T0)
    assert guide(s, T0)
    assert guide(s, T0 + 1000)
    assert not ambient(s, T0 + 1000)


# --- changing settings at runtime ----------------------------------------------------------


def test_set_policy_takes_effect_immediately_and_keeps_history() -> None:
    s = scheduler(glance=True, ambient=True)
    assert ambient(s, T0) and glance(s, T0)
    s.set_policy(WatchPolicy(glance=False, ambient=False, guide_max_fps=1.0))
    assert s.policy.levels == {Level.ON_DEMAND, Level.GUIDE}
    assert not ambient(s, T0 + AMBIENT_INTERVAL_MS)
    assert not glance(s, T0 + GLANCE_INTERVAL_MS, UUID_B)
    s.set_policy(WatchPolicy(glance=True, ambient=True, guide_max_fps=0.5))
    # Toggling off and on is not a way around the limits already running.
    assert not glance(s, T0 + 1000, UUID_A)
    assert not ambient(s, T0 + 1000)
    assert guide(s, T0)
    assert not guide(s, T0 + 1999)
    assert guide(s, T0 + 2000)


def test_unknown_trigger_is_refused() -> None:
    s = scheduler()
    assert not s.request(
        "user-ask-ish",  # type: ignore[arg-type]
        T0,
        guide_active=True,
        version_known=False,
        window_uuid=UUID_A,
    )
