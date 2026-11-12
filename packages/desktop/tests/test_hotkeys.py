"""Hotkey conflict planning (pure) and the portal / KGlobalAccel clients against fakes.

Every bus scenario runs twice: on an in-process hub (always) and on a private dbus-daemon
(skipped with a reason when the daemon is missing). Neither touches the real session bus.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from dbus_fast import Variant

from waddle_core.config import HotkeysConfig
from waddle_core.identity import APP_ID

sys.path.insert(0, str(Path(__file__).parent))
import fake_bus_services as fakes

from waddle_desktop.sensors import hotkeys as hk
from waddle_desktop.sensors.hotkeys import (
    ACTIONS,
    BindingPlan,
    BindStatus,
    Conflict,
    KGlobalAccelHotkeys,
    KGlobalAccelUnavailableError,
    PortalHotkeys,
    find_conflicts,
    plan_bindings,
    wanted_from_config,
)
from waddle_desktop.sensors.keys import KeyCombo, parse

DEFAULTS, _INVALID = wanted_from_config(HotkeysConfig())


@pytest.fixture(params=["inprocess", "daemon"])
def where(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    with fakes.transport(request.param, tmp_path) as target:
        if target is None:
            pytest.skip("dbus-daemon is not available: private-bus run skipped")
        yield target


# ---------------------------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------------------------


def test_actions_match_the_config_fields_in_order() -> None:
    assert list(ACTIONS) == list(HotkeysConfig.model_fields)
    assert ACTIONS["ask"] == "Ask Waddle a question"
    assert ACTIONS["push_to_talk"] == "Hold to talk to Waddle"
    assert all(text and len(text) <= 64 for text in ACTIONS.values())


def test_wanted_from_config_parses_the_defaults() -> None:
    assert _INVALID == {}
    assert list(DEFAULTS) == list(ACTIONS)
    assert DEFAULTS["ask"] == parse("Meta+Space")
    assert DEFAULTS["push_to_talk"] == parse("Meta+`")


def test_wanted_from_config_reports_bad_entries_without_echoing_them() -> None:
    config = HotkeysConfig(ask="Meta+Shift", pause="Hunter2Secret+W+Q", quiet="Escape")
    combos, invalid = wanted_from_config(config)
    assert set(invalid) == {"ask", "pause", "quiet"}
    assert set(combos) == set(ACTIONS) - set(invalid)
    assert "Hunter2Secret" not in " ".join(invalid.values())


def test_plan_without_conflicts_binds_everything_as_wanted() -> None:
    plans = plan_bindings(DEFAULTS, {})
    assert [p.action for p in plans] == list(ACTIONS)
    assert all(p.combo == p.wanted == DEFAULTS[p.action] for p in plans)
    assert all(p.conflict is None and p.alternatives == () for p in plans)


def test_plan_follows_actions_order_whatever_the_input_order() -> None:
    shuffled = {name: DEFAULTS[name] for name in reversed(list(ACTIONS))}
    assert [p.action for p in plan_bindings(shuffled, {})] == list(ACTIONS)
    assert [p.action for p in plan_bindings({"quiet": DEFAULTS["quiet"]}, {})] == ["quiet"]


def test_plan_offers_alternatives_for_a_conflict() -> None:
    conflict = Conflict("KRunner", "Run Command")
    plans = {p.action: p for p in plan_bindings(DEFAULTS, {"ask": conflict})}
    ask = plans["ask"]
    assert ask == BindingPlan(
        "ask",
        None,
        tuple(parse(t) for t in (
            "Meta+Shift+Space", "Meta+Alt+Space", "Meta+Ctrl+Space",
            "Meta+Alt+Shift+Space", "Meta+Ctrl+Shift+Space",
        )),
        conflict,
        parse("Meta+Space"),
    )  # fmt: skip
    assert plans["pause"].combo == parse("Meta+Shift+W")  # the others are untouched


def test_plan_alternatives_never_propose_a_key_another_action_wants() -> None:
    # Meta+W's first alternative would be Meta+Shift+W, which "pause" already wants.
    wanted = {**DEFAULTS, "ask": parse("Meta+W")}
    (ask,) = [p for p in plan_bindings(wanted, {"ask": Conflict("X", "Y")}) if p.action == "ask"]
    assert ask.alternatives
    assert not set(ask.alternatives) & set(wanted.values())
    assert parse("Meta+Shift+W") not in ask.alternatives


def test_plan_flags_two_waddle_actions_on_one_key() -> None:
    wanted = {**DEFAULTS, "quiet": DEFAULTS["pause"]}
    plans = {p.action: p for p in plan_bindings(wanted, {})}
    assert plans["pause"].combo == DEFAULTS["pause"]  # first in ACTIONS order keeps the key
    assert plans["quiet"].combo is None
    assert plans["quiet"].conflict == Conflict("Waddle", ACTIONS["pause"])
    assert plans["quiet"].alternatives


def test_plan_rejects_unknown_actions() -> None:
    with pytest.raises(ValueError, match="self_destruct"):
        plan_bindings({"self_destruct": parse("Meta+X")}, {})


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("KRunner", "KRunner"),
        ("  two\n\nlines\tand\x00nul\x1b[31m ", "two lines and nul [31m"),
        ("x" * 500, "x" * 63 + "…"),
        (b"bytes", ""),
        (None, ""),
        (["list"], ""),
        ("‮evil", "evil"),
    ],
)
def test_sanitize_friendly(raw: object, clean: str) -> None:
    result = hk.sanitize_friendly(raw)
    assert result == clean
    assert len(result) <= hk.MAX_FRIENDLY


def test_sender_token() -> None:
    assert hk.sender_token(":1.42") == "1_42"
    assert hk.sender_token(":1.1234.5") == "1_1234_5"


def test_bind_result_ok_only_when_bound() -> None:
    assert hk.BindResult(BindStatus.BOUND).ok
    assert not any(hk.BindResult(s).ok for s in BindStatus if s is not BindStatus.BOUND)


def test_the_kglobalaccel_fallback_is_a_visible_stub() -> None:
    stub = KGlobalAccelHotkeys(on_pressed=lambda a: None, on_released=lambda a: None)
    with pytest.raises(NotImplementedError):
        asyncio.run(stub.start({"ask": parse("Meta+Space")}))
    asyncio.run(stub.stop())


def test_conflict_queries_are_the_only_kglobalaccel_members() -> None:
    assert {"globalShortcutsByKey", "globalShortcutAvailable"} == hk.QUERY_MEMBERS
    source = Path(hk.__file__).read_text(encoding="utf-8")
    # No registering or key-setting member of KGlobalAccel appears as a string anywhere.
    for member in ("doRegister", "setShortcutKeys", "setShortcut", "unregister", "setForeign"):
        assert f'"{member}' not in source


# ---------------------------------------------------------------------------------------------
# Conflict detection against the fake KGlobalAccel
# ---------------------------------------------------------------------------------------------

SPACE = parse("Meta+Space").to_qt_int()
PAUSE = parse("Meta+Shift+W").to_qt_int()


async def _kga(where: Any) -> tuple[fakes.FakeKGlobalAccel, Any]:
    kga = fakes.FakeKGlobalAccel()
    await kga.start(where)
    return kga, await fakes.connector(where)()


def test_conflicts_are_found_and_named(where: Any) -> None:
    async def scenario() -> None:
        kga, bus = await _kga(where)
        kga.held[SPACE] = [
            ("run", "Run\nCommand" + "!" * 200, "org.kde.krunner.desktop", "KRunner")
        ]
        found = await find_conflicts(bus, DEFAULTS)
        assert list(found) == ["ask"]
        assert found["ask"].component_friendly == "KRunner"
        assert found["ask"].action_friendly.startswith("Run Command!")
        assert len(found["ask"].action_friendly) == hk.MAX_FRIENDLY
        # One read-only query per wanted key, each with that key's Qt integer.
        assert kga.queries == [("by-key", combo.to_qt_int()) for combo in DEFAULTS.values()]
        assert kga.members_called == {"globalShortcutsByKey"}

    asyncio.run(scenario())


@pytest.mark.parametrize("by_key", ["ok", "missing"])
def test_conflict_queries_never_start_kglobalaccel(where: Any, by_key: str) -> None:
    # A question must not start the daemon it is asked of (on a non-Plasma desktop a started
    # kglobalacceld would begin grabbing keys): every query carries the no-auto-start flag.
    async def scenario() -> None:
        kga, bus = await _kga(where)
        kga.by_key = by_key
        await find_conflicts(bus, DEFAULTS)
        assert len(kga.calls) >= len(DEFAULTS)
        assert kga.may_autostart == []

    asyncio.run(scenario())


def test_waddles_own_component_is_not_a_conflict(where: Any) -> None:
    async def scenario() -> None:
        kga, bus = await _kga(where)
        kga.held[SPACE] = [("ask", "Ask Waddle a question", APP_ID.upper(), "Waddle")]
        kga.held[PAUSE] = [
            ("pause", "Pause", APP_ID, "Waddle"),
            ("walk", "Walk Through Windows", "kwin", "KWin"),
        ]
        found = await find_conflicts(bus, DEFAULTS)
        assert found == {"pause": Conflict("KWin", "Walk Through Windows")}
        # Without the exemption both would be reported.
        assert set(await find_conflicts(bus, DEFAULTS, own_components=())) == {"ask", "pause"}

    asyncio.run(scenario())


@pytest.mark.parametrize("by_key", ["missing", "other-signature"])
def test_fallback_to_the_availability_query(where: Any, by_key: str) -> None:
    async def scenario() -> None:
        kga, bus = await _kga(where)
        kga.by_key = by_key
        kga.held[SPACE] = [("run", "Run Command", "org.kde.krunner.desktop", "KRunner")]
        kga.held[PAUSE] = [("pause", "Pause", APP_ID, "Waddle")]
        found = await find_conflicts(bus, DEFAULTS)
        assert found == {"ask": Conflict("", "")}  # taken, by an unnamed holder; ours is free
        kinds = [kind for kind, _key in kga.queries]
        # The failing query is tried once, not once per key.
        assert kinds.count("available") == len(DEFAULTS)
        assert kinds.count("by-key") == (1 if by_key == "other-signature" else 0)
        assert kga.members_called <= hk.QUERY_MEMBERS

    asyncio.run(scenario())


def test_no_answer_at_all_is_an_error_not_an_empty_result(where: Any) -> None:
    async def scenario() -> None:
        kga, bus = await _kga(where)
        kga.by_key = kga.available = "missing"
        with pytest.raises(KGlobalAccelUnavailableError):
            await find_conflicts(bus, DEFAULTS)
        await kga.stop()
        with pytest.raises(KGlobalAccelUnavailableError):
            await find_conflicts(bus, DEFAULTS, timeout=0.5)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------
# The portal
# ---------------------------------------------------------------------------------------------


class Rig:
    def __init__(self, where: Any, **options: Any) -> None:
        self.where = where
        self.events: list[tuple[str, str]] = []
        self.lost = 0
        self.portal = fakes.FakePortal()
        options.setdefault("call_timeout", 2.0)
        options.setdefault("bind_timeout", 2.0)
        self.hotkeys = PortalHotkeys(
            fakes.connector(where),
            on_pressed=lambda action: self.events.append(("pressed", action)),
            on_released=lambda action: self.events.append(("released", action)),
            on_lost=self._on_lost,
            **options,
        )

    def _on_lost(self) -> None:
        self.lost += 1

    async def bound(self) -> hk.BindResult:
        await self.portal.start(self.where)
        result = await self.hotkeys.start(DEFAULTS)
        assert result.status is BindStatus.BOUND
        return result


def portal_test(body: Callable[[Rig], Awaitable[None]], where: Any, **options: Any) -> None:
    async def scenario() -> None:
        rig = Rig(where, **options)
        try:
            await body(rig)
        finally:
            await rig.hotkeys.stop()

    asyncio.run(scenario())


def test_bind_flow_with_the_response_racing_the_reply(where: Any) -> None:
    async def body(rig: Rig) -> None:
        assert rig.portal.respond_before_return  # Response is sent before the method returns
        result = await rig.bound()
        assert result.ok and rig.hotkeys.active
        assert result.bound == tuple(ACTIONS) and result.missing == ()
        assert result.triggers == dict.fromkeys(ACTIONS, "Meta+Something")
        # Registered exactly once, before anything else was asked of the portal.
        assert rig.portal.registered == [APP_ID]
        assert rig.portal.order == ["Register", "CreateSession", "BindShortcuts", "ListShortcuts"]
        (shortcuts,) = rig.portal.bind_requests
        assert [ident for ident, _ in shortcuts] == list(ACTIONS)
        by_id = {ident: {k: v.value for k, v in opts.items()} for ident, opts in shortcuts}
        assert by_id["ask"] == {
            "description": "Ask Waddle a question",
            "preferred_trigger": "LOGO+space",
        }
        assert by_id["push_to_talk"]["preferred_trigger"] == "LOGO+grave"
        assert by_id["pause"]["preferred_trigger"] == "LOGO+SHIFT+w"
        # The session lives under our own unique name.
        assert re.fullmatch(
            r"/org/freedesktop/portal/desktop/session/1_\d+/waddle_[0-9a-f]{16}",
            rig.portal.session,
        )

    portal_test(body, where)


def test_tokens_are_random_and_never_reused(where: Any) -> None:
    async def body(rig: Rig) -> None:
        await rig.bound()
        first = [*rig.portal.tokens, rig.portal.session.rsplit("/", 1)[1]]
        await rig.hotkeys.stop()
        assert (await rig.hotkeys.start(DEFAULTS)).ok
        everything = [*rig.portal.tokens, rig.portal.session.rsplit("/", 1)[1], first[-1]]
        assert len(first) == 4 and len(everything) == 8
        assert len(set(everything)) == 8
        assert all(re.fullmatch(r"waddle_[0-9a-f]{16}", token) for token in everything)

    portal_test(body, where)


@pytest.mark.parametrize("mode", ["delayed", "legacy-path"])
def test_bind_flow_when_the_response_comes_later(where: Any, mode: str) -> None:
    async def body(rig: Rig) -> None:
        rig.portal.respond_before_return = False
        rig.portal.legacy_request_paths = mode == "legacy-path"
        result = await rig.bound()
        assert result.bound == tuple(ACTIONS)

    portal_test(body, where)


@pytest.mark.parametrize("registry", ["already", "missing"])
def test_registry_refusals_are_tolerated(where: Any, registry: str) -> None:
    async def body(rig: Rig) -> None:
        rig.portal.registry = registry
        assert (await rig.bound()).bound == tuple(ACTIONS)
        assert rig.portal.order[0] == "Register"  # still attempted first

    portal_test(body, where)


def test_no_portal_service(where: Any) -> None:
    async def body(rig: Rig) -> None:
        result = await rig.hotkeys.start(DEFAULTS)
        assert result.status is BindStatus.PORTAL_ABSENT
        assert not result.ok and result.bound == () and result.missing == tuple(ACTIONS)
        assert not rig.hotkeys.active

    portal_test(body, where)


def test_portal_without_global_shortcuts(where: Any) -> None:
    async def body(rig: Rig) -> None:
        rig.portal.has_shortcuts = False
        await rig.portal.start(where)
        assert (await rig.hotkeys.start(DEFAULTS)).status is BindStatus.PORTAL_ABSENT

    portal_test(body, where)


def test_no_bus_at_all() -> None:
    async def no_bus() -> Any:
        raise OSError("no session bus")

    async def scenario() -> None:
        hotkeys = PortalHotkeys(no_bus, on_pressed=lambda a: None, on_released=lambda a: None)
        assert (await hotkeys.start(DEFAULTS)).status is BindStatus.FAILED
        await hotkeys.stop()

    asyncio.run(scenario())


def test_user_cancels_the_dialog(where: Any) -> None:
    async def body(rig: Rig) -> None:
        rig.portal.bind_response = 1
        await rig.portal.start(where)
        result = await rig.hotkeys.start(DEFAULTS)
        assert result.status is BindStatus.CANCELLED
        assert result.missing == tuple(ACTIONS)
        assert not rig.hotkeys.active
        # Nothing is left behind: our session was closed, and its keys do nothing.
        await fakes.until(lambda: len(rig.portal.closed_sessions) == 1)
        rig.portal.sessions["/org/freedesktop/portal/desktop/session/x/y"] = {}
        rig.portal.activated("ask", rig.portal.closed_sessions[0])
        await fakes.settle()
        assert rig.events == []

    portal_test(body, where)


def test_session_creation_refused(where: Any) -> None:
    async def body(rig: Rig) -> None:
        rig.portal.create_response = 2
        await rig.portal.start(where)
        assert (await rig.hotkeys.start(DEFAULTS)).status is BindStatus.CANCELLED
        assert rig.portal.bind_requests == []

    portal_test(body, where)


def test_a_session_handle_that_is_not_ours_is_refused(where: Any) -> None:
    async def body(rig: Rig) -> None:
        rig.portal.foreign_session_handle = True
        await rig.portal.start(where)
        assert (await rig.hotkeys.start(DEFAULTS)).status is BindStatus.FAILED
        assert rig.portal.bind_requests == []

    portal_test(body, where)


def test_bind_times_out_and_withdraws_the_request(where: Any) -> None:
    async def body(rig: Rig) -> None:
        rig.portal.bind_silent = True
        await rig.portal.start(where)
        result = await rig.hotkeys.start(DEFAULTS)
        assert result.status is BindStatus.TIMEOUT
        assert not rig.hotkeys.active
        token = rig.portal.tokens[-1]
        assert len(rig.portal.closed_requests) == 1
        assert rig.portal.closed_requests[0].endswith("/" + token)
        assert len(rig.portal.closed_sessions) == 1

    portal_test(body, where, bind_timeout=0.3)


def test_shortcuts_the_user_unticked_are_reported_missing(where: Any) -> None:
    async def body(rig: Rig) -> None:
        rig.portal.refuse_ids = {"quiet", "ask"}
        result = await rig.bound()
        assert result.missing == ("ask", "quiet")
        assert result.bound == tuple(a for a in ACTIONS if a not in {"ask", "quiet"})

    portal_test(body, where)


def test_an_action_without_a_preferred_key_is_still_bound(where: Any) -> None:
    async def body(rig: Rig) -> None:
        await rig.portal.start(where)
        bindings: dict[str, KeyCombo | None] = {"ask": None, "pause": parse("Ctrl+Alt+F12")}
        result = await rig.hotkeys.start(bindings)
        assert result.bound == ("ask", "pause")
        (shortcuts,) = rig.portal.bind_requests
        by_id = dict(shortcuts)
        assert set(by_id["ask"]) == {"description"}
        assert by_id["pause"]["preferred_trigger"] == Variant("s", "CTRL+ALT+F12")

    portal_test(body, where)


def test_start_rejects_caller_bugs(where: Any) -> None:
    async def body(rig: Rig) -> None:
        with pytest.raises(ValueError, match="launch_missiles"):
            await rig.hotkeys.start({"launch_missiles": parse("Meta+X")})
        with pytest.raises(ValueError):
            await rig.hotkeys.start({})

    portal_test(body, where)


def test_press_and_release_edges_exactly_once(where: Any) -> None:
    async def body(rig: Rig) -> None:
        await rig.bound()
        portal = rig.portal
        portal.activated("push_to_talk")
        portal.activated("push_to_talk")  # key repeat / duplicate: ignored
        portal.activated("push_to_talk")
        await fakes.until(lambda: len(rig.events) >= 1)
        await fakes.settle()
        assert rig.events == [("pressed", "push_to_talk")]
        assert rig.hotkeys.pressed == {"push_to_talk"}

        portal.deactivated("push_to_talk")
        portal.deactivated("push_to_talk")  # a second release: ignored
        portal.deactivated("ask")  # a release with no press: ignored
        await fakes.until(lambda: len(rig.events) >= 2)
        await fakes.settle()
        assert rig.events == [("pressed", "push_to_talk"), ("released", "push_to_talk")]
        assert rig.hotkeys.pressed == set()

        # Two keys interleave independently, and a key can be pressed again after release.
        portal.activated("ask")
        portal.activated("push_to_talk")
        portal.deactivated("ask")
        portal.deactivated("push_to_talk")
        await fakes.until(lambda: len(rig.events) == 6)
        assert rig.events[2:] == [
            ("pressed", "ask"),
            ("pressed", "push_to_talk"),
            ("released", "ask"),
            ("released", "push_to_talk"),
        ]

    portal_test(body, where)


def test_signals_that_are_not_ours_are_ignored(where: Any) -> None:
    async def body(rig: Rig) -> None:
        await rig.bound()
        portal = rig.portal
        other = "/org/freedesktop/portal/desktop/session/9_9/other_app"
        portal.activated("ask", other)  # someone else's session
        portal.activated("not_an_action")
        portal.activated("x" * 5000)
        # A peer that is not the portal claims a key was pressed, addressed straight to us.
        imposter = await fakes.Imposter().start(where)
        client = rig.hotkeys._bus
        assert client is not None
        imposter.signal(
            fakes.PORTAL_PATH, fakes.SHORTCUTS, "Activated", "osta{sv}",
            [portal.session, "push_to_talk", 1, {}], destination=client.unique_name,
        )  # fmt: skip
        # Wrong shape from the real portal.
        portal.signal(
            fakes.PORTAL_PATH, fakes.SHORTCUTS, "Activated", "os", [portal.session, "ask"]
        )
        await fakes.settle(0.15)
        assert rig.events == []
        portal.activated("ask")  # ... and the real thing still works afterwards
        await fakes.until(lambda: rig.events == [("pressed", "ask")])

    portal_test(body, where)


def test_a_raising_callback_does_not_break_later_events(where: Any) -> None:
    async def scenario() -> None:
        seen: list[str] = []

        def pressed(action: str) -> None:
            seen.append(action)
            raise RuntimeError("consumer bug")

        portal = fakes.FakePortal()
        await portal.start(where)
        hotkeys = PortalHotkeys(fakes.connector(where), on_pressed=pressed, on_released=seen.append)
        assert (await hotkeys.start(DEFAULTS)).ok
        portal.activated("ask")
        portal.deactivated("ask")
        portal.activated("pause")
        await fakes.until(lambda: seen == ["ask", "ask", "pause"])
        await hotkeys.stop()

    asyncio.run(scenario())


def test_stop_releases_held_keys_and_closes_our_session(where: Any) -> None:
    async def body(rig: Rig) -> None:
        await rig.bound()
        session = rig.portal.session
        rig.portal.activated("push_to_talk")
        await fakes.until(lambda: rig.events == [("pressed", "push_to_talk")])
        await rig.hotkeys.stop()
        assert rig.events[-1] == ("released", "push_to_talk")  # the mic must not stay open
        assert not rig.hotkeys.active and rig.hotkeys.pressed == set()
        await fakes.until(lambda: rig.portal.closed_sessions == [session])
        rig.portal.sessions[session] = {}
        rig.portal.activated("ask", session)
        await fakes.settle()
        assert len(rig.events) == 2 and rig.lost == 0

    portal_test(body, where)


@pytest.mark.parametrize("how", ["portal-exits", "session-closed"])
def test_losing_the_portal_releases_held_keys_and_reports_it(where: Any, how: str) -> None:
    async def body(rig: Rig) -> None:
        await rig.bound()
        rig.portal.activated("push_to_talk")
        await fakes.until(lambda: rig.events == [("pressed", "push_to_talk")])
        if how == "portal-exits":
            await rig.portal.stop()
        else:
            rig.portal.close_session()
        await fakes.until(lambda: rig.lost == 1)
        assert rig.events == [("pressed", "push_to_talk"), ("released", "push_to_talk")]
        assert not rig.hotkeys.active
        if how == "session-closed":
            rig.portal.activated("ask")  # a dead session's keys do nothing
            await fakes.settle()
            assert len(rig.events) == 2 and rig.lost == 1
            # ... and the caller can simply start again.
            assert (await rig.hotkeys.start(DEFAULTS)).ok
            rig.portal.activated("ask", list(rig.portal.sessions)[-1])
            await fakes.until(lambda: rig.events[-1] == ("pressed", "ask"))

    portal_test(body, where)


def test_losing_the_bus_connection_releases_held_keys_and_reports_it(where: Any) -> None:
    # No NameOwnerChanged and no Deactivated can arrive on a dead connection: without this
    # a held push-to-talk key would leave the microphone open (I7).
    async def scenario() -> None:
        events: list[tuple[str, str]] = []
        lost: list[int] = []
        buses: list[Any] = []
        connect = fakes.connector(where)

        async def factory() -> Any:
            buses.append(await connect())
            return buses[-1]

        portal = fakes.FakePortal()
        await portal.start(where)
        hotkeys = PortalHotkeys(
            factory,
            on_pressed=lambda action: events.append(("pressed", action)),
            on_released=lambda action: events.append(("released", action)),
            on_lost=lambda: lost.append(1),
            call_timeout=2.0,
            bind_timeout=2.0,
        )
        try:
            assert (await hotkeys.start(DEFAULTS)).ok
            portal.activated("push_to_talk")
            await fakes.until(lambda: events == [("pressed", "push_to_talk")])
            buses[-1].disconnect()
            await fakes.until(lambda: lost == [1])
            assert events == [("pressed", "push_to_talk"), ("released", "push_to_talk")]
            assert not hotkeys.active and hotkeys.pressed == set()
            # A clean stop is not a loss, and the caller can start again afterwards.
            assert (await hotkeys.start(DEFAULTS)).ok
            await hotkeys.stop()
            await fakes.settle()
            assert lost == [1]
        finally:
            await hotkeys.stop()

    asyncio.run(scenario())
