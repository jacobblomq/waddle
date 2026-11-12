"""SessionWatcher against fake screen saver, logind and notification services.

Every scenario runs on an in-process hub (always) and on a private dbus-daemon (skipped with
a reason when the daemon is missing). The "system" bus is the same private bus: the watcher
takes both connections from injected factories. Nothing touches the real buses.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fake_bus_services as fakes

from waddle_desktop.sensors import session as session_module
from waddle_desktop.sensors.session import UNKNOWN, SessionSnapshot, SessionWatcher

# The complete set of D-Bus methods the session sensors may ever call. All are queries, plus
# AddMatch, which only changes what our own connection is sent.
READ_ONLY_ALLOWLIST = {
    "AddMatch",
    "GetNameOwner",
    "GetActive",
    "GetSession",
    "GetSessionByPID",
    "Get",
}


def snap(
    locked: bool, known: bool = True, dnd: bool = False, idle: bool = False
) -> SessionSnapshot:
    return SessionSnapshot(locked=locked, lock_known=known, dnd=dnd, idle=idle)


@pytest.fixture(params=["inprocess", "daemon"])
def where(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    with fakes.transport(request.param, tmp_path) as target:
        if target is None:
            pytest.skip("dbus-daemon is not available: private-bus run skipped")
        yield target


class Rig:
    def __init__(self, where: Any, **options: Any) -> None:
        self.where = where
        self.changes: list[SessionSnapshot] = []
        options.setdefault("session_id", "7")
        options.setdefault("pid", 4242)
        options.setdefault("call_timeout", 1.0)
        options.setdefault("lock_confirm_delay", 30.0)
        self.watcher = SessionWatcher(
            fakes.connector(where), fakes.connector(where), on_change=self.changes.append, **options
        )
        self.services: list[fakes.FakeService] = []

    async def serve[S: fakes.FakeService](self, service: S) -> S:
        await service.start(self.where)
        self.services.append(service)
        return service

    @property
    def last(self) -> SessionSnapshot:
        return self.changes[-1]

    async def expect(self, snapshot: SessionSnapshot) -> None:
        try:
            await fakes.until(lambda: bool(self.changes) and self.changes[-1] == snapshot)
        except TimeoutError:
            raise AssertionError(f"wanted {snapshot}, have {self.changes}") from None
        assert self.watcher.snapshot == snapshot

    async def quiet(self, seconds: float = 0.12) -> int:
        """Wait, then return how many changes were delivered in total."""
        await fakes.settle(seconds)
        return len(self.changes)


def run(body: Callable[[Rig], Awaitable[None]], where: Any, **options: Any) -> None:
    async def scenario() -> None:
        rig = Rig(where, **options)
        try:
            await body(rig)
        finally:
            await rig.watcher.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------------------------


def test_nothing_running_means_lock_unknown(where: Any) -> None:
    async def body(rig: Rig) -> None:
        assert rig.watcher.snapshot == UNKNOWN
        first = await rig.watcher.start()
        assert first == snap(False, known=False) == rig.watcher.snapshot
        assert rig.changes == [first]  # the baseline is delivered exactly once

    run(body, where)


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("path", ["/ScreenSaver", "/org/freedesktop/ScreenSaver"])
def test_initial_state_from_the_screen_saver(where: Any, active: bool, path: str) -> None:
    async def body(rig: Rig) -> None:
        await rig.serve(fakes.FakeScreenSaver(active, paths=(path,)))
        assert await rig.watcher.start() == snap(active)
        assert rig.changes == [snap(active)]

    run(body, where)


@pytest.mark.parametrize("locked", [True, False])
def test_initial_state_from_logind(where: Any, locked: bool) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=locked, idle=True))
        assert await rig.watcher.start() == snap(locked, idle=True)
        assert rig.changes == [snap(locked, idle=True)]
        assert ("/org/freedesktop/login1", logind.MANAGER, "GetSession") in logind.calls
        assert "GetSessionByPID" not in logind.members_called  # the id was enough

    run(body, where)


def test_logind_session_found_by_pid_when_there_is_no_session_id(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=True))
        logind.session_ids.clear()
        logind.pids[4242] = logind.SESSION_PATH
        assert await rig.watcher.start() == snap(True)
        assert "GetSession" not in logind.members_called  # an empty id is not even asked

    run(body, where, session_id="")


def test_logind_session_found_through_the_users_display_session(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=True))
        logind.session_ids.clear()
        logind.display = ("37", logind.SESSION_PATH)
        assert await rig.watcher.start() == snap(True)

    run(body, where, session_id="no-such-session")


@pytest.mark.parametrize("bad", ["/org/freedesktop/login1/user/_1000", "/", "/org/freedesktop"])
def test_a_logind_answer_that_is_not_a_session_path_is_not_used(where: Any, bad: str) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=True))
        logind.session_ids["7"] = bad
        logind.pids[4242] = bad
        logind.display = ("37", bad)
        first = await rig.watcher.start()
        assert first == snap(False, known=False)
        assert (bad, fakes.PROPERTIES, "Get") not in logind.calls  # never used as an object

    run(body, where)


@pytest.mark.parametrize(
    "bad",
    [
        "/org/freedesktop/login1/session/a b",
        "/org/freedesktop/login1/session/",
        "/org/freedesktop/login1/session/" + "x" * 300,
        "/org/freedesktop/login1/session/../../../ScreenSaver",
        42,
        None,
    ],
)
def test_session_path_validation(bad: object) -> None:
    assert SessionWatcher._is_session_path(bad) is False
    assert SessionWatcher._is_session_path("/org/freedesktop/login1/session/_37") is True


def test_locked_is_the_or_of_the_sources(where: Any) -> None:
    async def body(rig: Rig) -> None:
        saver = await rig.serve(fakes.FakeScreenSaver(False))
        logind = await rig.serve(fakes.FakeLogind(locked=True))
        assert await rig.watcher.start() == snap(True)
        saver.set_active(True)
        logind.set_hint("LockedHint", False)
        await fakes.settle()
        assert rig.watcher.snapshot == snap(True)  # the screen saver still says locked
        saver.set_active(False)
        await rig.expect(snap(False))
        assert rig.changes == [snap(True), snap(False)]

    run(body, where)


def test_initial_dnd(where: Any) -> None:
    async def body(rig: Rig) -> None:
        await rig.serve(fakes.FakeNotifications(inhibited=True))
        assert await rig.watcher.start() == snap(False, known=False, dnd=True)

    run(body, where)


def test_a_notification_server_without_the_extension_means_no_dnd(where: Any) -> None:
    async def body(rig: Rig) -> None:
        await rig.serve(fakes.FakeNotifications(inhibited=None))
        assert (await rig.watcher.start()).dnd is False

    run(body, where)


# ---------------------------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------------------------


def test_active_changed(where: Any) -> None:
    async def body(rig: Rig) -> None:
        saver = await rig.serve(fakes.FakeScreenSaver(False))
        await rig.watcher.start()
        saver.set_active(True)
        await rig.expect(snap(True))
        saver.set_active(False)
        await rig.expect(snap(False))
        assert rig.changes == [snap(False), snap(True), snap(False)]

    run(body, where)


def test_locked_hint_change(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind())
        await rig.watcher.start()
        logind.set_hint("LockedHint", True)
        await rig.expect(snap(True))
        logind.set_hint("LockedHint", False)
        await rig.expect(snap(False))
        assert rig.changes == [snap(False), snap(True), snap(False)]

    run(body, where)


def test_idle_hint_change(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind())
        await rig.watcher.start()
        logind.set_hint("IdleHint", True)
        await rig.expect(snap(False, idle=True))
        logind.set_hint("IdleHint", False)
        await rig.expect(snap(False))

    run(body, where)


def test_dnd_toggles(where: Any) -> None:
    async def body(rig: Rig) -> None:
        notifications = await rig.serve(fakes.FakeNotifications(False))
        await rig.watcher.start()
        notifications.set_inhibited(True)
        await rig.expect(snap(False, known=False, dnd=True))
        notifications.set_inhibited(False)
        await rig.expect(snap(False, known=False))
        # A server that only invalidates the property makes us read it once.
        gets = notifications.calls.count((notifications.PATH, fakes.PROPERTIES, "Get"))
        notifications.set_inhibited(True, invalidate_only=True)
        await rig.expect(snap(False, known=False, dnd=True))
        assert notifications.calls.count((notifications.PATH, fakes.PROPERTIES, "Get")) == gets + 1

    run(body, where)


def test_nothing_is_emitted_when_nothing_changed(where: Any) -> None:
    async def body(rig: Rig) -> None:
        saver = await rig.serve(fakes.FakeScreenSaver(False))
        logind = await rig.serve(fakes.FakeLogind())
        notifications = await rig.serve(fakes.FakeNotifications(False))
        await rig.watcher.start()
        assert rig.changes == [snap(False)]
        saver.set_active(False)
        logind.set_hint("LockedHint", False)
        logind.set_hint("IdleHint", False)
        notifications.set_inhibited(False)
        logind.emit("Unlock")
        assert await rig.quiet() == 1
        saver.set_active(True)  # ... while a real change still gets through
        await rig.expect(snap(True))
        saver.set_active(True)
        logind.set_hint("LockedHint", True)  # a second source agreeing changes nothing
        assert await rig.quiet() == 2

    run(body, where)


def test_nothing_is_polled(where: Any) -> None:
    async def body(rig: Rig) -> None:
        services = [
            await rig.serve(fakes.FakeScreenSaver(False)),
            await rig.serve(fakes.FakeLogind()),
            await rig.serve(fakes.FakeNotifications(False)),
        ]
        await rig.watcher.start()
        before = [len(service.calls) for service in services]
        await fakes.settle(0.4)
        assert [len(service.calls) for service in services] == before

    run(body, where)


# ---------------------------------------------------------------------------------------------
# The Lock signal fail-safe
# ---------------------------------------------------------------------------------------------


def test_lock_signal_locks_before_any_property_is_read(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=False))
        await rig.watcher.start()
        assert rig.changes == [snap(False)]
        logind.hold_properties = asyncio.Event()  # from now on logind answers no Get
        gets = logind.calls.count((logind.SESSION_PATH, fakes.PROPERTIES, "Get"))
        logind.emit("Lock")
        await rig.expect(snap(True))
        # Locked on the signal alone: LockedHint still says False and nothing was re-read.
        assert logind.locked is False
        assert logind.calls.count((logind.SESSION_PATH, fakes.PROPERTIES, "Get")) == gets

    run(body, where)


def test_lock_signal_during_start_is_reported_before_start_finishes(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=False))
        logind.hold_properties = asyncio.Event()
        starting = asyncio.ensure_future(rig.watcher.start())
        await fakes.until(lambda: "Get" in logind.members_called)  # subscribed, now reading
        assert rig.changes == []
        logind.emit("Lock")
        await fakes.until(lambda: rig.changes == [snap(True)])
        assert not starting.done()
        logind.release_properties()
        assert await starting == snap(True)  # the latch outlives the (stale) "unlocked" read

    run(body, where)


def test_an_unconfirmed_lock_signal_does_not_blind_forever(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=False))
        await rig.serve(fakes.FakeScreenSaver(False))
        await rig.watcher.start()
        logind.emit("Lock")
        await rig.expect(snap(True))
        await rig.expect(snap(False))  # after the delayed re-read, the sources decide
        assert rig.changes == [snap(False), snap(True), snap(False)]

    run(body, where, lock_confirm_delay=0.2)


def test_a_confirmed_lock_stays_locked_after_the_re_read(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=False))
        await rig.watcher.start()
        logind.emit("Lock")
        logind.set_hint("LockedHint", True)
        await rig.expect(snap(True))
        assert await rig.quiet(0.4) == 2 and rig.last == snap(True)
        logind.set_hint("LockedHint", False)
        await rig.expect(snap(False))

    run(body, where, lock_confirm_delay=0.1)


def test_unlock_signal_and_unlocked_reports_drop_the_latch(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=False))
        saver = await rig.serve(fakes.FakeScreenSaver(False))
        await rig.watcher.start()
        logind.emit("Lock")
        await rig.expect(snap(True))
        logind.emit("Unlock")
        await rig.expect(snap(False))
        logind.emit("Lock")
        await rig.expect(snap(True))
        saver.set_active(False)  # the locker says: not locked
        await rig.expect(snap(False))

    run(body, where)


def test_lock_signals_for_other_sessions_are_ignored(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=False))
        await rig.watcher.start()
        logind.emit("Lock", logind.OTHER_SESSION_PATH)
        logind.set_hint("LockedHint", True, path=logind.OTHER_SESSION_PATH)
        assert await rig.quiet() == 1 and rig.last == snap(False)

    run(body, where)


# ---------------------------------------------------------------------------------------------
# Services coming and going
# ---------------------------------------------------------------------------------------------


def test_all_lock_sources_vanishing_makes_the_lock_unknown(where: Any) -> None:
    async def body(rig: Rig) -> None:
        saver = await rig.serve(fakes.FakeScreenSaver(False))
        logind = await rig.serve(fakes.FakeLogind(locked=False))
        await rig.watcher.start()
        assert rig.last == snap(False)
        await saver.stop()
        assert await rig.quiet() == 1  # logind still answers for the lock
        assert rig.watcher.snapshot == snap(False)
        await logind.stop()
        await rig.expect(snap(False, known=False))

    run(body, where)


def test_a_vanished_locker_takes_its_locked_state_with_it(where: Any) -> None:
    async def body(rig: Rig) -> None:
        saver = await rig.serve(fakes.FakeScreenSaver(True))
        await rig.watcher.start()
        assert rig.last == snap(True)
        await saver.stop()
        await rig.expect(snap(False, known=False))  # unknown, which the gate treats as blind

    run(body, where)


def test_a_source_that_appears_later_is_picked_up(where: Any) -> None:
    async def body(rig: Rig) -> None:
        await rig.watcher.start()
        assert rig.last == snap(False, known=False)
        saver = await rig.serve(fakes.FakeScreenSaver(True))
        await rig.expect(snap(True))
        await saver.stop()
        await rig.expect(snap(False, known=False))
        await rig.serve(fakes.FakeLogind(locked=False, idle=True))
        await rig.expect(snap(False, idle=True))
        await rig.serve(fakes.FakeNotifications(True))
        await rig.expect(snap(False, idle=True, dnd=True))

    run(body, where)


def test_dnd_falls_back_to_off_when_the_notification_server_leaves(where: Any) -> None:
    async def body(rig: Rig) -> None:
        notifications = await rig.serve(fakes.FakeNotifications(True))
        await rig.watcher.start()
        assert rig.last.dnd is True
        await notifications.stop()
        await rig.expect(snap(False, known=False, dnd=False))

    run(body, where)


def test_services_that_are_not_running_are_not_called() -> None:
    """Calling an absent name would make the bus try to start it; we only ask who owns it."""

    async def scenario() -> None:
        hub = fakes.InProcessHub()
        watcher = SessionWatcher(hub.connector(), hub.connector(), on_change=lambda s: None)
        await watcher.start()
        await watcher.stop()
        assert hub.unrouted == []
        assert hub.match_rules  # it did subscribe

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------------------------


def test_forged_signals_from_another_peer_are_ignored(where: Any) -> None:
    async def body(rig: Rig) -> None:
        await rig.serve(fakes.FakeScreenSaver(True))
        logind = await rig.serve(fakes.FakeLogind(locked=True))
        await rig.serve(fakes.FakeNotifications(False))
        await rig.watcher.start()
        assert rig.last == snap(True)
        imposter = await fakes.Imposter().start(where)
        # locked is the OR of two sources, so the snapshot alone would hide one forged
        # "unlocked" that got through: every source's own value must stay as it was.
        before = dict(rig.watcher._values)
        assert before == {"screensaver": True, "locked-hint": True, "idle-hint": False,
                          "inhibited": False}  # fmt: skip
        session_side, login_side = rig.watcher._session_side, rig.watcher._login_side
        assert session_side is not None and login_side is not None
        me, me_login = session_side.bus.unique_name, login_side.bus.unique_name
        props = [logind.SESSION, {"LockedHint": fakes.Variant("b", False)}, []]
        imposter.signal(
            "/ScreenSaver", "org.freedesktop.ScreenSaver", "ActiveChanged", "b", [False], me
        )
        imposter.signal(
            logind.SESSION_PATH, fakes.PROPERTIES, "PropertiesChanged", "sa{sv}as", props, me_login
        )
        imposter.signal(logind.SESSION_PATH, logind.SESSION, "Unlock", "", [], me_login)
        imposter.signal(
            "/org/freedesktop/Notifications", fakes.PROPERTIES, "PropertiesChanged", "sa{sv}as",
            ["org.freedesktop.Notifications", {"Inhibited": fakes.Variant("b", True)}, []], me,
        )  # fmt: skip
        imposter.signal(
            "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameOwnerChanged", "sss",
            ["org.freedesktop.ScreenSaver", "x", ""], me,
        )  # fmt: skip
        assert await rig.quiet(0.15) == 1
        assert rig.watcher.snapshot == snap(True)
        assert rig.watcher._values == before
        assert login_side.owners["org.freedesktop.login1"] is not None

    run(body, where)


def test_a_forged_unlock_does_not_lift_the_lock_latch(where: Any) -> None:
    # After a Lock signal only the latch says "locked" until the hints follow. A forged
    # Unlock or "LockedHint false" in that window would open the eyes on a locked screen.
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=False))
        await rig.watcher.start()
        logind.emit("Lock")
        await rig.expect(snap(True))
        imposter = await fakes.Imposter().start(where)
        login_side = rig.watcher._login_side
        assert login_side is not None
        me_login = login_side.bus.unique_name
        props = [logind.SESSION, {"LockedHint": fakes.Variant("b", False)}, []]
        imposter.signal(logind.SESSION_PATH, logind.SESSION, "Unlock", "", [], me_login)
        imposter.signal(
            logind.SESSION_PATH, fakes.PROPERTIES, "PropertiesChanged", "sa{sv}as", props, me_login
        )
        await fakes.settle(0.15)
        assert rig.watcher.snapshot == snap(True)

    run(body, where, lock_confirm_delay=30.0)


def test_the_lock_latch_is_only_dropped_after_the_sources_were_read_again(where: Any) -> None:
    # The locker set LockedHint but its PropertiesChanged never reached us: when the latch
    # runs out, the fresh read must keep the screen locked.
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind(locked=False))
        await rig.watcher.start()
        logind.emit("Lock")
        await rig.expect(snap(True))
        logind.locked = True  # silently: no signal
        await fakes.settle(0.4)
        assert rig.watcher._latch is False  # the latch has run out
        assert rig.watcher.snapshot == snap(True)
        assert rig.changes[-1] == snap(True) and snap(False) not in rig.changes[1:]

    run(body, where, lock_confirm_delay=0.1)


def test_malformed_signals_from_the_real_services_are_ignored(where: Any) -> None:
    async def body(rig: Rig) -> None:
        saver = await rig.serve(fakes.FakeScreenSaver(True))
        logind = await rig.serve(fakes.FakeLogind(locked=True))
        await rig.watcher.start()
        saver.signal("/ScreenSaver", "org.freedesktop.ScreenSaver", "ActiveChanged", "s", ["no"])
        saver.signal("/ScreenSaver", "org.freedesktop.ScreenSaver", "ActiveChanged", "", [])
        logind.signal(
            logind.SESSION_PATH, fakes.PROPERTIES, "PropertiesChanged", "sa{sv}as",
            [logind.SESSION, {"LockedHint": fakes.Variant("s", "false")}, []],
        )  # fmt: skip
        logind.signal(logind.SESSION_PATH, fakes.PROPERTIES, "PropertiesChanged", "s", ["x"])
        logind.signal(
            logind.SESSION_PATH, fakes.PROPERTIES, "PropertiesChanged", "sa{sv}as",
            ["org.example.Other", {"LockedHint": fakes.Variant("b", False)}, []],
        )  # fmt: skip
        assert await rig.quiet(0.15) == 1
        assert rig.watcher.snapshot == snap(True)
        saver.set_active(False)  # the handler survived all of that
        logind.set_hint("LockedHint", False)
        await rig.expect(snap(False))

    run(body, where)


def test_a_raising_callback_is_swallowed(where: Any) -> None:
    async def scenario() -> None:
        seen: list[SessionSnapshot] = []

        def on_change(snapshot: SessionSnapshot) -> None:
            seen.append(snapshot)
            raise RuntimeError("consumer bug")

        saver = fakes.FakeScreenSaver(False)
        await saver.start(where)
        watcher = SessionWatcher(
            fakes.connector(where), fakes.connector(where), on_change=on_change
        )
        try:
            assert await watcher.start() == snap(False)
            saver.set_active(True)
            await fakes.until(lambda: seen == [snap(False), snap(True)])
            saver.set_active(False)
            await fakes.until(lambda: len(seen) == 3)
        finally:
            await watcher.stop()

    asyncio.run(scenario())


def test_stop_stops_emissions(where: Any) -> None:
    async def body(rig: Rig) -> None:
        saver = await rig.serve(fakes.FakeScreenSaver(False))
        logind = await rig.serve(fakes.FakeLogind())
        await rig.watcher.start()
        await rig.watcher.stop()
        assert rig.watcher.snapshot == UNKNOWN
        saver.set_active(True)
        logind.emit("Lock")
        logind.set_hint("LockedHint", True)
        await saver.stop()
        assert await rig.quiet(0.15) == 1
        await rig.watcher.stop()  # stopping twice is fine
        # ... and it can be started again, getting a fresh baseline.
        assert await rig.watcher.start() == snap(True)
        assert rig.changes == [snap(False), snap(True)]

    run(body, where)


def test_stop_cancels_the_pending_lock_re_read(where: Any) -> None:
    async def body(rig: Rig) -> None:
        logind = await rig.serve(fakes.FakeLogind())
        await rig.watcher.start()
        logind.emit("Lock")
        await rig.expect(snap(True))
        await rig.watcher.stop()
        assert await rig.quiet(0.3) == 2  # the 0.1 s re-read never fired

    run(body, where, lock_confirm_delay=0.1)


def test_missing_buses_are_tolerated() -> None:
    async def no_bus() -> Any:
        raise OSError("no bus")

    async def scenario() -> None:
        changes: list[SessionSnapshot] = []
        watcher = SessionWatcher(no_bus, no_bus, on_change=changes.append)
        assert await watcher.start() == snap(False, known=False)
        assert changes == [snap(False, known=False)]
        await watcher.stop()

    asyncio.run(scenario())


def test_only_the_system_bus_missing() -> None:
    async def no_bus() -> Any:
        raise OSError("no bus")

    async def scenario() -> None:
        hub = fakes.InProcessHub()
        saver = fakes.FakeScreenSaver(True)
        await saver.start(hub)
        changes: list[SessionSnapshot] = []
        watcher = SessionWatcher(hub.connector(), no_bus, on_change=changes.append)
        assert await watcher.start() == snap(True)
        await watcher.stop()

    asyncio.run(scenario())


def test_losing_the_bus_connection_makes_its_sources_unknown() -> None:
    async def scenario() -> None:
        hub = fakes.InProcessHub()
        await fakes.FakeScreenSaver(True).start(hub)
        await fakes.FakeNotifications(True).start(hub)
        changes: list[SessionSnapshot] = []
        watcher = SessionWatcher(hub.connector(), hub.connector(), on_change=changes.append)
        assert await watcher.start() == snap(True, dnd=True)
        assert watcher._session_side is not None
        watcher._session_side.bus.disconnect()  # the daemon went away under us
        await fakes.until(lambda: changes[-1] == snap(False, known=False))
        await watcher.stop()

    asyncio.run(scenario())


def test_a_read_overtaken_by_a_signal_is_discarded() -> None:
    """A reply can be *applied* after a newer signal was; the stale value must not win."""

    async def scenario() -> None:
        watcher = SessionWatcher(on_change=lambda s: None)
        watcher._running = True
        epoch = watcher._epoch(session_module._LOCKED)
        watcher._bump(session_module._LOCKED)  # what a PropertiesChanged handler does
        watcher._values[session_module._LOCKED] = True
        watcher._apply(session_module._LOCKED, epoch, False)  # the older reply resumes
        assert watcher.snapshot.locked is True
        watcher._apply(session_module._LOCKED, watcher._epoch(session_module._LOCKED), False)
        assert watcher.snapshot.locked is False

    asyncio.run(scenario())


def test_defaults_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_ID", "c12")
    watcher = SessionWatcher(on_change=lambda s: None)
    assert watcher._session_id == "c12" and watcher._pid == os.getpid()
    monkeypatch.delenv("XDG_SESSION_ID")
    assert SessionWatcher(on_change=lambda s: None)._session_id == ""


def test_snapshot_is_frozen() -> None:
    with pytest.raises(AttributeError):
        UNKNOWN.locked = True  # type: ignore[misc]


# ---------------------------------------------------------------------------------------------
# Read-only by construction
# ---------------------------------------------------------------------------------------------


def test_callable_members_are_within_the_read_only_allowlist() -> None:
    assert session_module.CALLABLE_MEMBERS <= READ_ONLY_ALLOWLIST
    assert {m.member for m in session_module.METHODS} == session_module.CALLABLE_MEMBERS
    # Spot-check that the allowlist itself has not been widened by accident.
    never = "Lock Unlock Inhibit UnInhibit SetIdleHint SetLockedHint SimulateUserActivity"
    for forbidden in (*never.split(), "SetActive", "Set", "Terminate", "Kill", "Notify"):
        assert forbidden not in READ_ONLY_ALLOWLIST


def test_the_module_can_only_send_what_is_in_its_method_table() -> None:
    """Structural: one place builds messages, from a table that is fully listed."""
    tree = ast.parse(Path(session_module.__file__).read_text(encoding="utf-8"))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def enclosing_function(node: ast.AST) -> str | None:
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node.name
        return None

    def calls(name: str) -> list[ast.Call]:
        return [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Name) and n.func.id == name)
                or (isinstance(n.func, ast.Attribute) and n.func.attr == name)
            )
        ]

    # Messages are built in exactly one function ...
    assert [enclosing_function(c) for c in calls("Message")] == ["_send"]
    # ... which is the only place that puts anything on a bus ...
    assert [enclosing_function(c) for c in calls("call")] == ["_send"]
    assert calls("send") == [] and calls("call_sync") == [] and calls("get_proxy_object") == []
    # ... and only for methods from the module-level table, every one of which is listed.
    method_defs = calls("_Method")
    assert all(enclosing_function(c) is None for c in method_defs)
    assert len(method_defs) == len(session_module.METHODS)
    literal_members = {c.args[1].value for c in method_defs if isinstance(c.args[1], ast.Constant)}
    assert literal_members == session_module.CALLABLE_MEMBERS


def test_send_refuses_a_method_that_is_not_in_the_table() -> None:
    async def scenario() -> None:
        hub = fakes.InProcessHub()
        logind = fakes.FakeLogind()
        await logind.start(hub)
        watcher = SessionWatcher(hub.connector(), hub.connector(), on_change=lambda s: None)
        bus = await hub.connector()()
        rogue = session_module._Method("org.freedesktop.login1.Session", "Lock", "")
        with pytest.raises(AssertionError):
            await watcher._send(bus, "org.freedesktop.login1", logind.SESSION_PATH, rogue, [])
        assert logind.calls == []

    asyncio.run(scenario())


def test_a_full_session_only_ever_calls_allowlisted_members(where: Any) -> None:
    async def body(rig: Rig) -> None:
        saver = await rig.serve(fakes.FakeScreenSaver(False))
        logind = await rig.serve(fakes.FakeLogind())
        logind.session_ids.clear()  # walk the whole resolution chain
        logind.display = ("37", logind.SESSION_PATH)
        notifications = await rig.serve(fakes.FakeNotifications(False))
        await rig.watcher.start()
        logind.emit("Lock")
        await rig.expect(snap(True))
        logind.set_hint("LockedHint", True)
        saver.set_active(True)
        notifications.set_inhibited(True, invalidate_only=True)
        await rig.expect(snap(True, dnd=True))
        logind.emit("Unlock")
        logind.set_hint("LockedHint", False)
        saver.set_active(False)
        await rig.expect(snap(False, dnd=True))
        await fakes.settle(0.3)  # let the delayed re-read after Lock happen too
        called = set().union(*(service.members_called for service in rig.services))
        assert called == {"GetActive", "GetSession", "GetSessionByPID", "Get"}
        assert called <= READ_ONLY_ALLOWLIST
        # Watching must never start a service: every query carries the no-auto-start flag.
        assert [service.may_autostart for service in rig.services] == [[]] * len(rig.services)
        if isinstance(where, fakes.InProcessHub):
            assert where.match_rules  # AddMatch is the only thing sent to the bus itself

    run(body, where, lock_confirm_delay=0.1)
