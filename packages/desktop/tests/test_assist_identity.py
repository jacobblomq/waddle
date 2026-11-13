"""The identity service: refinement in the background, stale results dropped, honest versions."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import assist_helpers as h

from waddle_desktop.assist.identity_service import (
    NOTHING,
    PROBE_RETRY_SECONDS,
    IdentityService,
)
from waddle_desktop.assist.shared_exec import SharedExec
from waddle_desktop.identity.cache import AppIdentity, IdentityCache
from waddle_desktop.identity.packaging import Packaging
from waddle_desktop.identity.packs import Pack
from waddle_desktop.identity.version import parse_version
from waddle_desktop.identity.waterfall import Probe, Refined
from waddle_desktop.sandbox.readonly_exec import Reason

CANARY = "CANARY-2b9c"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class GatedRefine:
    """A refinement that waits to be released, and records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (app name, caption)
        self.gates: list[asyncio.Event] = []
        self.active = 0
        self.most_active = 0

    async def __call__(
        self,
        base: AppIdentity,
        *,
        caption: str,
        pack: Pack | None,
        probe: Probe | None,
        cache: IdentityCache | None,
    ) -> Refined:
        self.calls.append((base.name, caption))
        gate = asyncio.Event()
        self.gates.append(gate)
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        try:
            await gate.wait()
        finally:
            self.active -= 1
        version = parse_version("9.9")
        return Refined(replace(base, version=version, source="version-probe", confidence=0.9))


class Rig:
    def __init__(self, tmp_path: Path, **options: object) -> None:
        self.senses = h.SensesRig()
        self.fake = h.FakeExec()
        self.clock = Clock()
        self.changes = 0
        self.cache = IdentityCache(tmp_path / "identity.db")
        self.service = IdentityService(
            eyes=lambda: self.senses.senses.state,
            packs=h.fixture_packs(),
            cache=self.cache,
            sandbox=SharedExec(lambda: self.fake, clock=self.clock),
            on_change=self._changed,
            clock=self.clock,
            **options,  # type: ignore[arg-type]
        )
        self.senses.senses.add_listener(self.service.on_eyes)

    def _changed(self) -> None:
        self.changes += 1

    async def refined(self) -> None:
        """Wait until the window in front has a finished refinement."""
        await h.until(lambda: self.service.settled)


def good_exe(tmp_path: Path) -> str:
    exe = tmp_path / "good"
    exe.write_bytes(b"\x7fELF")
    return str(exe)


# ---- the answer --------------------------------------------------------------------------


async def test_a_packaged_version_is_passed_on_with_the_packs_name(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.senses.focus(h.win())
    await rig.refined()
    app, pack, needs_user, about = rig.service.current()
    assert app is not None and pack is not None
    assert (app.name, app.version, app.version_source) == ("Good App", "5.3.1", "dpkg")
    assert (pack.id, needs_user, about) == ("good", False, ())
    assert rig.senses.identified == 1  # identify() ran once, in the senses; not again here
    assert rig.fake.calls == []  # a known version needs no probe


async def test_a_version_below_the_confidence_floor_is_not_passed_on(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.senses.identity = h.ident(version="5.3.1", confidence=0.5, source="appimage-name")
    await rig.senses.focus(h.win(caption="untitled"))
    await rig.refined()
    app = rig.service.current().app
    assert app is not None and app.name == "Good App"
    assert (app.version, app.version_source) == (None, "unknown")

    rig.senses.identity = h.ident(version="5.3.1", confidence=0.6, source="appimage-name")
    await rig.senses.focus(h.win(h.U2, caption="untitled", pid=11))
    await rig.refined()
    app = rig.service.current().app
    assert app is not None and app.version == "5.3.1"


async def test_no_name_means_no_app_context_at_all(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.senses.identity = h.ident(name="", exe="", version=None)
    await rig.senses.focus(h.win(cls="mystery", caption="?"))
    await rig.refined()
    assert rig.service.current().app is None


async def test_nothing_is_worked_out_for_a_window_the_gate_refuses(tmp_path: Path) -> None:
    refine = GatedRefine()
    rig = Rig(tmp_path, refine=refine)
    await rig.senses.focus(h.win(cls="KeePassXC", caption=f"{CANARY} - KeePassXC"))
    await h.settle()
    assert rig.service.current() == NOTHING
    assert refine.calls == []
    await rig.senses.focus(h.win(h.U2, pid=11), session=h.LOCKED)
    await h.settle()
    assert rig.service.current() == NOTHING and refine.calls == []
    rig.senses.senses.session_changed(h.UNLOCKED)
    await h.until(lambda: refine.calls)
    rig.senses.senses.set_paused(True)
    assert rig.service.current() == NOTHING  # paused: what was known is not handed out
    await rig.service.stop()


async def test_before_refinement_finishes_the_base_identity_answers(tmp_path: Path) -> None:
    refine = GatedRefine()
    rig = Rig(tmp_path, refine=refine)
    await rig.senses.focus(h.win())
    await h.until(lambda: refine.calls)
    app, pack, needs_user, _ = rig.service.current()  # does not wait for the refinement
    assert app is not None and (app.name, app.version) == ("Good App", "5.3.1")
    assert pack is not None and not needs_user
    refine.gates[0].set()
    await rig.refined()
    app = rig.service.current().app
    assert app is not None and app.version == "9.9"


# ---- stale results and concurrency -------------------------------------------------------


async def test_a_result_for_a_window_no_longer_in_front_is_dropped(tmp_path: Path) -> None:
    refine = GatedRefine()
    rig = Rig(tmp_path, refine=refine)
    await rig.senses.focus(h.win(h.U1, caption="first"))
    await h.until(lambda: len(refine.calls) == 1)
    rig.senses.identity = h.ident("other", exe="/usr/bin/other", version="1.0")
    await rig.senses.focus(h.win(h.U2, cls="other", caption="second", pid=11))
    # The focus change was not held up: the first refinement is still running.
    assert refine.active == 1 and len(refine.calls) == 1
    with h.captured_logs() as logs:
        refine.gates[0].set()
        await h.until(lambda: len(refine.calls) == 2)
    assert rig.changes == 0  # the stale result was not stored
    assert any("identity_stale_dropped" in line for line in logs)
    assert refine.calls == [("good", "first"), ("other", "second")]
    refine.gates[1].set()
    await rig.refined()
    app = rig.service.current().app
    assert app is not None and (app.name, app.version) == ("other", "9.9")
    assert refine.most_active == 1  # one refinement (so one probe) at a time
    # Back on the first window it is refined afresh: the dropped result was not kept.
    rig.senses.identity = h.ident()
    await rig.senses.focus(h.win(h.U1, caption="first"))
    await h.until(lambda: len(refine.calls) == 3)
    await rig.service.stop()


async def test_a_title_change_makes_the_running_result_stale(tmp_path: Path) -> None:
    refine = GatedRefine()
    rig = Rig(tmp_path, refine=refine)
    await rig.senses.focus(h.win(caption="one"))
    await h.until(lambda: len(refine.calls) == 1)
    rig.senses.senses.target_changed(h.win(caption="two"))
    refine.gates[0].set()
    await h.until(lambda: len(refine.calls) == 2)
    assert rig.changes == 0 and refine.calls[1] == ("good", "two")
    refine.gates[1].set()
    await rig.refined()
    assert rig.changes == 1
    # The same window, title and identity again: nothing new to work out.
    rig.senses.senses.target_changed(h.win(caption="two"))
    await h.settle()
    assert len(refine.calls) == 2
    await rig.service.stop()


async def test_a_window_that_becomes_blocked_drops_its_result(tmp_path: Path) -> None:
    refine = GatedRefine()
    rig = Rig(tmp_path, refine=refine)
    await rig.senses.focus(h.win(cls="firefox", caption="News"))
    await h.until(lambda: refine.calls)
    rig.senses.senses.target_changed(h.win(cls="firefox", caption="x (Private Browsing)"))
    refine.gates[0].set()
    await h.settle()
    assert rig.changes == 0 and rig.service.current() == NOTHING
    assert len(refine.calls) == 1  # and the private title was never matched or probed


async def test_stop_ends_a_running_refinement(tmp_path: Path) -> None:
    refine = GatedRefine()
    rig = Rig(tmp_path, refine=refine)
    await rig.senses.focus(h.win())
    await h.until(lambda: refine.active == 1)
    await rig.service.stop()
    assert refine.active == 0 and rig.changes == 0
    rig.senses.senses.target_changed(h.win(caption="later"))
    await h.settle()
    assert len(refine.calls) == 1


# ---- the real waterfall ------------------------------------------------------------------


async def test_the_probe_runs_in_the_sandbox_and_its_version_is_used(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    exe = good_exe(tmp_path)
    rig.senses.identity = h.ident(exe=exe, version=None, packaging=Packaging.OPT)
    rig.fake.results = [h.result("good 7.2\n")]
    await rig.senses.focus(h.win(caption="untitled"))
    await rig.refined()
    app = rig.service.current().app
    assert app is not None
    assert (app.version, app.version_source) == ("7.2", "version-probe")
    assert rig.fake.of("probe") == [("probe", exe, "-V")]


async def test_a_probe_timeout_is_not_remembered_as_no_version(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    exe = good_exe(tmp_path)
    rig.senses.identity = h.ident(exe=exe, version=None, packaging=Packaging.OPT)
    rig.fake.results = [h.result("", Reason.TIMEOUT), h.result("good 7.2\n")]
    await rig.senses.focus(h.win(caption="untitled"))
    await rig.refined()
    current = rig.service.current()
    assert current.needs_user and current.about_path == ("Help", "About Good App")
    assert current.app is not None and current.app.version is None
    assert len(rig.fake.of("probe")) == 1

    # Soon after, the slow program is left alone...
    rig.senses.senses.target_changed(h.win(caption="untitled 2"))
    await rig.refined()
    assert len(rig.fake.of("probe")) == 1 and rig.service.current().needs_user
    # ...and later it is asked again, which a cached "no version" would have prevented.
    rig.clock.now += PROBE_RETRY_SECONDS + 1
    rig.senses.senses.target_changed(h.win(caption="untitled 3"))
    await rig.refined()
    assert len(rig.fake.of("probe")) == 2
    current = rig.service.current()
    assert not current.needs_user
    assert current.app is not None and current.app.version is not None


async def test_a_probe_answer_is_not_asked_for_twice_even_without_a_cache(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.cache.close()  # an unusable cache file: every lookup misses
    exe = good_exe(tmp_path)
    rig.senses.identity = h.ident(exe=exe, version=None, packaging=Packaging.OPT)
    rig.fake.results = [h.result("no version here", Reason.FAILED)]
    await rig.senses.focus(h.win(caption="a"))
    await rig.refined()
    rig.senses.senses.target_changed(h.win(caption="b"))
    await rig.refined()
    assert len(rig.fake.of("probe")) == 1


async def test_without_a_working_sandbox_nothing_is_probed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.fake.available = False
    rig.senses.identity = h.ident(exe=good_exe(tmp_path), version=None, packaging=Packaging.OPT)
    await rig.senses.focus(h.win(caption="scene - Good 4.1"))
    await rig.refined()
    assert rig.fake.of("probe") == []
    app = rig.service.current().app  # the title pattern still works
    assert app is not None and (app.version, app.version_source) == ("4.1", "caption")


async def test_a_web_app_in_a_browser_tab_is_named_without_a_version(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.senses.identity = h.ident("firefox", exe="/usr/bin/firefox", version="144.0")
    await rig.senses.focus(h.win(cls="firefox", caption="Docs — Mozilla Firefox"))
    await rig.refined()
    app, pack, _, _ = rig.service.current()
    assert app is not None and pack is not None
    assert (pack.id, app.name, app.version) == ("browser", "Browser", "144.0")
    rig.senses.senses.target_changed(
        h.win(cls="firefox", caption="Inbox (3) - Example Mail — Mozilla Firefox")
    )
    # Until the new title is refined, the tab is not mistaken for the old result.
    app = rig.service.current().app
    assert app is not None and app.name == "Browser"
    await rig.refined()
    app, pack, needs_user, _ = rig.service.current()
    assert app is not None and pack is not None
    assert (pack.id, app.name, app.version, needs_user) == ("webmail", "Example Mail", None, False)


# ---- asking the user ---------------------------------------------------------------------


async def test_the_users_answer_is_used_and_remembered_for_this_binary(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.fake.available = False
    exe = good_exe(tmp_path)
    rig.senses.identity = h.ident(exe=exe, version=None, packaging=Packaging.OPT)
    await rig.senses.focus(h.win(caption="untitled"))
    await rig.refined()
    assert rig.service.current().needs_user
    assert rig.service.apply_user_version("no idea") is False
    assert rig.service.current().needs_user
    with h.captured_logs() as logs:
        assert rig.service.apply_user_version(f"Good App 6.1.2 {CANARY}") is True
    assert CANARY not in "".join(logs)
    current = rig.service.current()
    assert not current.needs_user and current.app is not None
    assert (current.app.version, current.app.version_source) == ("6.1.2", "user")
    assert rig.changes == 2
    # Another window of the same program: the answer is found again, not asked again.
    await rig.senses.focus(h.win(h.U2, caption="other file"))
    await rig.refined()
    current = rig.service.current()
    assert not current.needs_user and current.app is not None
    assert current.app.version == "6.1.2"


async def test_a_flatpak_answer_is_remembered_for_the_session(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.senses.identity = h.ident(
        exe="/app/bin/good", version=None, packaging=Packaging.FLATPAK, source="flatpak"
    )
    await rig.senses.focus(h.win(caption="untitled"))
    await rig.refined()
    assert rig.service.apply_user_version("6.1") is True
    rig.senses.senses.target_changed(h.win(caption="renamed"))
    await rig.refined()
    current = rig.service.current()
    assert not current.needs_user and current.app is not None and current.app.version == "6.1"


async def test_an_unasked_answer_changes_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.senses.focus(h.win())
    await rig.refined()
    assert rig.service.apply_user_version("1.0") is False  # the version is already known
    app = rig.service.current().app
    assert app is not None and app.version == "5.3.1"
    rig.senses.senses.set_paused(True)
    assert rig.service.apply_user_version("1.0") is False  # and never while blind


async def test_packs_and_their_errors_are_kept_for_the_doctor(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.service.packs.get("good") is not None
    assert rig.service.packs.errors  # the fixture directory holds broken packs on purpose


@pytest.mark.parametrize("version", ["5.3.1"])
async def test_the_listener_copes_with_many_quick_focus_changes(
    tmp_path: Path, version: str
) -> None:
    refine = GatedRefine()
    rig = Rig(tmp_path, refine=refine)
    await rig.senses.focus(h.win(caption="t0"))
    for index in range(1, 30):
        rig.senses.senses.target_changed(h.win(caption=f"t{index}"))
    await h.until(lambda: refine.calls)
    while refine.active or len(refine.calls) < 2:
        for gate in refine.gates:
            gate.set()
        await asyncio.sleep(0)
    await h.until(lambda: rig.changes == 1)
    assert refine.calls == [("good", "t0"), ("good", "t29")]  # the ones in between never ran
    assert refine.most_active == 1
