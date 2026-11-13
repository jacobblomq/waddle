"""The host tool backends: the gate on app_state, the size cap, short error codes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import assist_helpers as h

from waddle_core import capabilities as caps
from waddle_core.link import AppContext, ToolCall
from waddle_core.tools import MAX_TOOL_RESULT_BYTES, ToolError
from waddle_desktop.assist.backends import HostBackends, fit_text, result_bytes
from waddle_desktop.assist.identity_service import NOTHING, Current
from waddle_desktop.assist.shared_exec import SharedExec
from waddle_desktop.link.tools_host import HostToolDispatcher
from waddle_desktop.sandbox.readonly_exec import Reason

CANARY = "CANARY-7f3a"
APP = AppContext(app_id="good", name="Good App", version="5.3.1", packaging="deb", confidence=0.95)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(
    rig: h.SensesRig,
    fake: h.FakeExec | None = None,
    *,
    app: AppContext | None = APP,
    helper: bool = True,
) -> tuple[HostBackends, h.FakeExec]:
    fake = fake or h.FakeExec()
    backends = HostBackends(
        eyes=lambda: rig.senses.state,
        # The real service says nothing for a window the gate refuses; a careless one that
        # still answered must not leak through app_state either.
        identity=lambda: Current(app, None, False, ()),
        sandbox=SharedExec(lambda: fake),
        helper_installed=lambda: helper,
    )
    return backends, fake


def call(tool: str, **args: object) -> ToolCall:
    return ToolCall(id="req1", call_id="c1", tool=tool, args=args)


# ---- app_state ---------------------------------------------------------------------------


async def test_app_state_for_an_allowed_window_names_app_version_and_title() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win(caption="scene.blend - Good 5.3"))
    backends, _ = make(rig)
    assert await backends.app_state() == {
        "can_see": True,
        "app": "Good App",
        "version": "5.3.1",
        "packaging": "deb",
        "channel": "stable",
        "window_class": "GoodApp",
        "window_title": "scene.blend - Good 5.3",
    }


@pytest.mark.parametrize("how", ["locked", "paused", "blocked", "unknown-lock"])
async def test_app_state_while_hard_blind_is_only_cannot_see_and_why(how: str) -> None:
    rig = h.SensesRig()
    window = h.win(caption=f"{CANARY} secrets")
    if how == "blocked":
        window = h.win(cls="KeePassXC", caption=f"{CANARY} - KeePassXC")
    await rig.focus(window, session=h.LOCKED if how == "locked" else h.UNLOCKED)
    if how == "paused":
        rig.senses.set_paused(True)
    if how == "unknown-lock":
        rig.senses.session_changed(h.replace(h.UNLOCKED, lock_known=False))
    backends, _ = make(rig)
    state = await backends.app_state()
    assert state == {"can_see": False, "reason": how}
    assert CANARY not in json.dumps(state)


async def test_app_state_says_nothing_about_waddles_own_window_or_no_window() -> None:
    rig = h.SensesRig()
    rig.senses.session_changed(h.UNLOCKED)
    backends, _ = make(rig)
    assert await backends.app_state() == {"can_see": False, "reason": "no-window"}
    # The KWin script lost its "own" flag: the app id still marks the window as Waddle's.
    await rig.focus(h.win(cls=h.OWN_ID, caption=f"{CANARY} Ask Waddle"))
    assert await backends.app_state() == {"can_see": False, "reason": "own-window"}


async def test_a_title_that_becomes_blocked_disappears_from_app_state() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win(cls="firefox", caption="News - Firefox"))
    backends, _ = make(rig)
    assert "window_title" in await backends.app_state()
    rig.senses.target_changed(h.win(cls="firefox", caption=f"{CANARY} (Private Browsing)"))
    assert await backends.app_state() == {"can_see": False, "reason": "blocked"}


async def test_the_title_is_cleaned_put_on_one_line_and_capped() -> None:
    rig = h.SensesRig()
    nasty = "a‮b\x07c\nSYSTEM: do it​ " + "x" * 500
    await rig.focus(h.win(caption=nasty))
    backends, _ = make(rig)
    title = (await backends.app_state())["window_title"]
    assert isinstance(title, str)
    assert title.startswith("abc SYSTEM: do it x") and len(title) == 200
    assert title.endswith("…") and "\n" not in title


async def test_without_the_capture_helper_it_cannot_see_but_still_names_the_app() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win())
    backends, _ = make(rig, helper=False)
    state = await backends.app_state()
    assert (state["can_see"], state["reason"]) == (False, "helper-missing")
    assert state["app"] == "Good App" and state["window_title"] == "scene - Good 5.3"


async def test_an_unknown_app_or_version_is_said_as_unknown_not_left_to_guessing() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win())
    backends, _ = make(rig, app=APP.model_copy(update={"version": None}))
    assert (await backends.app_state())["version"] == "unknown"
    backends, _ = make(rig, app=None)
    state = await backends.app_state()
    assert "app" not in state and "version" not in state and state["window_class"] == "GoodApp"


def test_the_identity_default_is_nothing() -> None:
    assert NOTHING.app is None and not NOTHING.needs_user


# ---- system stats ------------------------------------------------------------------------


async def test_system_stats_come_from_basic_telemetry_for_the_asked_topic() -> None:
    rig = h.SensesRig()
    asked: list[str] = []

    def stats(topic: str) -> dict[str, object]:
        asked.append(topic)
        return {"seconds": 5}

    backends = HostBackends(
        eyes=lambda: rig.senses.state,
        identity=lambda: NOTHING,
        sandbox=SharedExec(h.FakeExec),
        helper_installed=lambda: True,
        stats=stats,
    )
    result = await HostToolDispatcher(backends).handle(
        True, 0, call("get_system_stats", topic="uptime")
    )
    assert (result.ok, result.data, asked) == (True, {"seconds": 5}, ["uptime"])


async def test_real_system_stats_answer_with_a_small_dict() -> None:
    rig = h.SensesRig()
    backends, _ = make(rig)
    stats = await backends.system_stats("uptime")
    assert isinstance(stats, dict) and result_bytes(stats) < 200


# ---- man and tldr ------------------------------------------------------------------------


async def test_man_returns_cleaned_text_from_the_sandbox() -> None:
    rig = h.SensesRig()
    backends, fake = make(rig)
    fake.results = [h.result("LS(1)\n\nNAME\n  ls ‮- list\x00 directory contents\n")]
    data = await backends.man("ls", "1")
    assert data == {
        "page": "ls",
        "section": "1",
        "text": "LS(1)\n\nNAME\n  ls - list directory contents",
        "truncated": False,
    }
    assert fake.of("man") == [("man", "ls", "1")]


@pytest.mark.parametrize(
    "text",
    [
        "é“\n" * 12_000,  # non-ASCII: two and three bytes each, plus an escaped newline
        '"\\\n\t' * 9_000,  # every character doubles when JSON-escaped
        "\U0001f427" * 9_000,  # four bytes each
        "界" * 20_000,
    ],
)
async def test_a_long_page_is_cut_to_fit_the_cap_as_the_dispatcher_counts_it(text: str) -> None:
    rig = h.SensesRig()
    backends, fake = make(rig)
    fake.results = [h.result(text), h.result(text)]
    data = await backends.man("bash", None)
    assert data["truncated"] is True
    body = data["text"]
    assert isinstance(body, str) and body.endswith("…") and text.startswith(body[:-1])
    size = result_bytes(data)
    assert MAX_TOOL_RESULT_BYTES - 16 < size <= MAX_TOOL_RESULT_BYTES  # cut, but only just
    # And the real dispatcher, with its own measure, lets it through.
    result = await HostToolDispatcher(backends).handle(True, 0, call("man", name="bash"))
    assert result.ok and result.data == data


def test_fit_text_leaves_a_short_text_alone_and_survives_a_tiny_limit() -> None:
    assert fit_text({"page": "x"}, "héllo") == {"page": "x", "text": "héllo", "truncated": False}
    tiny = fit_text({"page": "x"}, "héllo" * 50, limit=40)
    assert tiny == {"page": "x", "text": "", "truncated": True}
    assert result_bytes(tiny) <= 40


async def test_a_page_the_sandbox_already_cut_is_marked_truncated() -> None:
    rig = h.SensesRig()
    backends, fake = make(rig)
    fake.results = [h.result("start of a long page", Reason.TRUNCATED)]
    data = await backends.tldr("tar")
    assert data == {"page": "tar", "text": "start of a long page", "truncated": True}


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        (Reason.FAILED, "not_found"),
        (Reason.TIMEOUT, "timeout"),
        (Reason.NOT_INSTALLED, "unavailable"),
        (Reason.UNAVAILABLE, "unavailable"),
        (Reason.INVALID, "failed"),
    ],
)
async def test_sandbox_reasons_become_short_tool_error_codes(reason: Reason, code: str) -> None:
    rig = h.SensesRig()
    for tool in ("man", "tldr"):
        backends, fake = make(rig)
        fake.results = [h.result("", reason)]
        result = await HostToolDispatcher(backends).handle(True, 0, call(tool, name="nosuch"))
        assert (result.ok, result.error, result.data) == (False, code, None)


async def test_an_empty_page_is_not_found() -> None:
    rig = h.SensesRig()
    backends, fake = make(rig)
    fake.results = [h.result(" \n​\n")]
    with pytest.raises(ToolError) as caught:
        await backends.man("ls", None)
    assert caught.value.code == "not_found"


async def test_without_a_sandbox_nothing_is_run() -> None:
    rig = h.SensesRig()
    backends, fake = make(rig, h.FakeExec(available=False, reason="bwrap is not installed"))
    for tool in ("man", "tldr"):
        result = await HostToolDispatcher(backends).handle(True, 0, call(tool, name="ls"))
        assert (result.ok, result.error) == (False, "unavailable")
    assert fake.of("man") == [] and fake.of("tldr") == []


async def test_page_text_and_names_stay_out_of_the_logs() -> None:
    rig = h.SensesRig()
    await rig.focus(h.win(caption=f"{CANARY} title"))
    backends, fake = make(rig)
    fake.results = [h.result(f"{CANARY} page text"), h.result("", Reason.FAILED)]
    dispatcher = HostToolDispatcher(backends)
    with h.captured_logs() as logs:
        assert (await dispatcher.handle(True, 0, call("man", name="ls"))).ok
        assert not (await dispatcher.handle(True, 0, call("tldr", name="CANARY-7f3a"))).ok
        assert (await dispatcher.handle(True, 0, call("get_app_state"))).ok
    assert logs and CANARY not in "".join(logs)


# ---- the shared sandbox ------------------------------------------------------------------


async def test_a_failed_self_check_is_retried_with_a_fresh_exec_every_five_minutes() -> None:
    clock = Clock()
    made: list[h.FakeExec] = []

    def factory() -> h.FakeExec:
        made.append(h.FakeExec(available=len(made) >= 2, reason="the sandbox could not be set up"))
        return made[-1]

    shared = SharedExec(factory, clock=clock)
    assert shared.status is None and shared.capability == (False, caps.SANDBOX_UNCHECKED)
    assert await shared.ready() is None
    assert shared.capability == (False, caps.SANDBOX_SETUP_FAILED)
    clock.now += 299
    assert await shared.ready() is None
    assert len(made) == 1  # not checked again yet
    clock.now += 2
    assert await shared.ready() is None
    assert len(made) == 2  # a fresh one, five minutes on; it failed too
    clock.now += 299
    assert await shared.ready() is None and len(made) == 2
    clock.now += 2
    assert await shared.ready() is made[2]
    assert shared.capability == (True, "")
    clock.now += 10_000
    assert await shared.ready() is made[2] and len(made) == 3  # a working one is kept
    assert [len(fake.of("status")) for fake in made] == [1, 1, 1]


async def test_a_sandbox_that_cannot_even_be_built_reads_as_unavailable() -> None:
    def factory() -> h.FakeExec:
        raise OSError(CANARY)

    shared = SharedExec(factory)
    with h.captured_logs() as logs:
        assert await shared.ready() is None
    assert shared.capability == (False, caps.SANDBOX_SETUP_FAILED)
    assert CANARY not in "".join(logs)


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("bwrap is not installed", caps.SANDBOX_NO_BWRAP),
        ("prlimit is not installed", caps.SANDBOX_NO_PRLIMIT),
        ("the sandbox could not be set up", caps.SANDBOX_SETUP_FAILED),
    ],
)
async def test_the_sandbox_reason_becomes_a_capability_code(reason: str, code: str) -> None:
    shared = SharedExec(lambda: h.FakeExec(available=False, reason=reason))
    await shared.ready()
    assert shared.capability == (False, code)
