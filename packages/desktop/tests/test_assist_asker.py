"""Asking: one look, the context the gate allows, one open question, clean relays."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import assist_helpers as h
import fake_brain as fb

from waddle_core.link import (
    MAX_IMAGE_BYTES,
    AnswerDelta,
    AnswerDone,
    AnswerError,
    AppContext,
    Ask,
    Cancel,
    ModelStatus,
    Prewarm,
    Role,
    State,
    WindowContext,
)
from waddle_desktop.assist import asker as asker_mod
from waddle_desktop.assist.asker import AskController, Sent, frame_images
from waddle_desktop.assist.identity_service import Current
from waddle_desktop.link.client import BrainDownError, BrainLink, Image, LinkState
from waddle_desktop.link.tools_host import HostToolDispatcher
from waddle_desktop.sensors.capture_client import Blind, CaptureError, Unchanged

TOKEN = "ab" * 32
CANARY = "CANARY-51d9"
QUESTION = f"What does this {CANARY} button do?"
APP = AppContext(app_id="good", name="Good App", version="5.3.1", confidence=0.95)


class NoTools:
    async def app_state(self) -> dict[str, object]:
        return {}

    async def system_stats(self, topic: str) -> dict[str, object]:
        return {}

    async def man(self, name: str, section: str | None) -> dict[str, object]:
        return {}

    async def tldr(self, name: str) -> dict[str, object]:
        return {}


class Rig:
    """A fake brain, a real link, real senses over a fake helper, and the controller."""

    def __init__(self, root: Path) -> None:
        run = root / "run"
        run.mkdir(mode=0o700)
        token_file = root / "link-token"
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)
        self.brain = fb.FakeBrain(run / "brain.sock", TOKEN)
        self.eyes = h.SensesRig()
        self.events: list[tuple[str, object]] = []
        self.sent: list[Sent] = []
        self.states: list[LinkState] = []
        self.app: AppContext | None = APP
        self.asker: AskController
        self.link = BrainLink(
            run / "brain.sock",
            token_file,
            client_version="0.3-test",
            tools=HostToolDispatcher(NoTools()),
            on_state=lambda state: self.asker.link_state(state),
        )
        self.asker = AskController(
            link=self.link,
            senses=self.eyes.senses,
            identity=lambda: Current(self.app, None, False, ()),
            on_delta=lambda text: self.events.append(("delta", text)),
            on_done=lambda sources, role: self.events.append(("done", (sources, role))),
            on_error=lambda code: self.events.append(("error", code)),
            on_state=self.states.append,
            on_sent=self.sent.append,
        )

    async def up(self) -> None:
        await self.brain.start()
        self.link.start()
        await h.until(lambda: self.link.state.status == "up")

    async def asked(self, count: int = 1) -> Ask:
        await h.until(lambda: len(self.brain.of_type(Ask)) >= count)
        return self.brain.of_type(Ask)[count - 1]

    def images(self) -> list[tuple[str, str, str, bytes]]:
        return [m for m in self.brain.received if isinstance(m, tuple)]

    async def send(self, message: object) -> None:
        await self.brain.sessions[-1].send(message)

    @property
    def ended(self) -> list[tuple[str, object]]:
        return [e for e in self.events if e[0] != "delta"]

    async def __aenter__(self) -> Rig:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.link.stop()
        await self.brain.stop()
        await self.eyes.senses.stop()


@pytest.fixture
def root() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="wa-"))  # short: a socket path must fit 108 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def rig(root: Path) -> Rig:
    return Rig(root)


# ---- a sighted question ------------------------------------------------------------------


async def test_a_question_carries_one_look_and_the_allowed_context(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win(caption="scene.blend - Good 5.3"))
        await rig.asker.ask(f"  {QUESTION}\n")
        ask = await rig.asked()
        # Both images went first, as the helper encoded them, then the ask naming them.
        assert [(m[2], m[3]) for m in rig.images()] == [
            ("image/jpeg", h.JPEG),
            ("image/png", h.PNG),
        ]
        assert [type(m) for m in rig.brain.received][-1] is Ask
        assert [ref.kind for ref in ask.images] == ["overview", "crop"]
        assert [ref.image_id for ref in ask.images] == [m[1] for m in rig.images()]
        assert (ask.text, ask.can_see, ask.blind_reason) == (QUESTION, True, None)
        assert ask.app == APP
        assert ask.window == WindowContext(
            resource_class="GoodApp",
            title="scene.blend - Good 5.3",
            width=1600,
            height=1200,
            scale=1.0,
            fullscreen=False,
            xwayland=False,
        )
        assert len(rig.eyes.eye.captures()) == 1  # exactly one look, on demand
        assert rig.eyes.frames == []  # and the frame was not kept for a closed eyes panel
        assert rig.sent == [Sent(True, None, 2, len(h.JPEG) + len(h.PNG), APP, True)]
        assert rig.asker.busy


async def test_the_answer_is_relayed_cleaned_with_newlines_kept(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win())
        await rig.asker.ask(QUESTION)
        ask = await rig.asked()
        await rig.send(AnswerDelta(id=ask.id, text="Line one\nLine‮ two\x07​"))
        await rig.send(AnswerDelta(id=ask.id, text="​\x00"))  # nothing printable
        await rig.send(AnswerDelta(id=ask.id, text="<b>three</b>"))
        await rig.send(AnswerDone(id=ask.id, sources=("screen", "app"), role="smart_model"))
        await h.until(lambda: rig.ended)
        assert rig.events == [
            ("delta", "Line one\nLine two"),
            ("delta", "<b>three</b>"),  # markup is passed as text; the UI shows plain text
            ("done", (("screen", "app"), "smart_model")),
        ]
        assert not rig.asker.busy


async def test_a_brain_error_is_relayed_as_its_code(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win())
        await rig.asker.ask(QUESTION)
        ask = await rig.asked()
        await rig.send(AnswerError(id=ask.id, code="model_timeout", waking=True))
        await h.until(lambda: rig.ended)
        assert rig.events == [("error", "model_timeout")]


async def test_link_state_changes_are_relayed(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.send(State(id="s1", waking="smart_model"))
        await h.until(lambda: rig.states and rig.states[-1].waking == "smart_model")
        assert [s.status for s in rig.states] == ["connecting", "up", "up"]


# ---- blind questions ---------------------------------------------------------------------


@pytest.mark.parametrize("how", ["locked", "paused", "blocked", "unknown-lock"])
async def test_a_blind_question_has_no_image_no_title_and_no_app(rig: Rig, how: str) -> None:
    async with rig:
        await rig.up()
        window = h.win(caption=f"{CANARY} secret title")
        if how == "blocked":
            window = h.win(cls="KeePassXC", caption=f"{CANARY} - KeePassXC")
        await rig.eyes.focus(window, session=h.LOCKED if how == "locked" else h.UNLOCKED)
        if how == "paused":
            rig.eyes.senses.set_paused(True)
        if how == "unknown-lock":
            rig.eyes.senses.session_changed(h.replace(h.UNLOCKED, lock_known=False))
        await rig.asker.ask("what am I looking at?")
        ask = await rig.asked()
        assert (ask.can_see, ask.blind_reason, ask.images) == (False, how, ())
        assert ask.window is None and ask.app is None
        assert rig.images() == []
        assert rig.eyes.eye.captures() == []  # the helper was never asked to look
        assert CANARY not in ask.model_dump_json()
        assert rig.sent == [Sent(False, how, 0, 0, None, False)]


async def test_no_focused_window_is_said_as_such(rig: Rig) -> None:
    async with rig:
        await rig.up()
        rig.eyes.senses.session_changed(h.UNLOCKED)
        await rig.asker.ask("hello?")
        ask = await rig.asked()
        assert (ask.can_see, ask.blind_reason) == (False, "no-window")
        assert ask.window is None and ask.app is None


async def test_without_the_helper_the_question_still_names_app_and_window(rig: Rig) -> None:
    async with rig:
        await rig.up()
        rig.eyes.unavailable = "missing"
        await rig.eyes.focus(h.win())
        await rig.asker.ask(QUESTION)
        ask = await rig.asked()
        assert (ask.can_see, ask.blind_reason, ask.images) == (False, "helper-missing", ())
        assert ask.app == APP and ask.window is not None
        assert ask.window.title == "scene - Good 5.3"
        assert (ask.window.width, ask.window.height) == (800, 600)  # from the geometry


@pytest.mark.parametrize(
    ("outcome", "reason", "described"),
    [
        (Blind("blocked"), "blocked", False),  # the helper's own gate said no: say nothing
        (Blind("locked"), "locked", False),
        (Blind("latched"), "paused", False),
        (Blind("not_found"), "no-window", True),
        (CaptureError("lock_unknown"), "unknown-lock", False),
        (CaptureError("kwin_timeout"), "capture-failed", True),
        (Unchanged("0" * 16), "capture-failed", True),
    ],
)
async def test_what_the_helper_refuses_becomes_a_blind_reason(
    rig: Rig, outcome: object, reason: str, described: bool
) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win(caption=f"{CANARY} title"))
        rig.eyes.eye.results = [outcome]  # type: ignore[list-item]
        await rig.asker.ask("what is this?")
        ask = await rig.asked()
        assert (ask.can_see, ask.blind_reason, ask.images) == (False, reason, ())
        assert (ask.window is not None, ask.app is not None) == (described, described)
        assert rig.images() == []


async def test_a_frame_of_a_window_that_went_private_meanwhile_is_not_sent(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win(cls="firefox", caption="News"))
        rig.eyes.eye.gate = asyncio.Event()
        asking = asyncio.ensure_future(rig.asker.ask("what is this?"))
        await h.until(rig.eyes.eye.captures)
        rig.eyes.senses.target_changed(h.win(cls="firefox", caption=f"{CANARY} Private Browsing"))
        rig.eyes.eye.gate.set()
        await asking
        ask = await rig.asked()
        assert (ask.can_see, ask.blind_reason) == (False, "blocked")
        assert ask.window is None and ask.app is None and rig.images() == []


def test_an_image_over_the_link_limit_is_left_out_not_cut() -> None:
    big = b"\x89PNG\r\n\x1a\n" + bytes(MAX_IMAGE_BYTES)
    assert frame_images(h.frame()) == [
        ("overview", "image/jpeg", h.JPEG),
        ("crop", "image/png", h.PNG),
    ]
    assert frame_images(h.frame(crop=big)) == [("overview", "image/jpeg", h.JPEG)]
    assert frame_images(h.frame(overview=b"", crop=big)) == []


# ---- one open question -------------------------------------------------------------------


async def test_a_new_question_ends_the_one_before_it_silently(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win())
        await rig.asker.ask("first?")
        first = await rig.asked()
        await rig.send(AnswerDelta(id=first.id, text="first answer"))
        await h.until(lambda: rig.events)
        await rig.asker.ask("second?")
        second = await rig.asked(2)
        await h.until(lambda: rig.brain.of_type(Cancel))
        assert [c.id for c in rig.brain.of_type(Cancel)] == [first.id]
        # Late words and the end of the first answer are not shown under the second question.
        await rig.send(AnswerDelta(id=first.id, text="late"))
        await rig.send(AnswerError(id=first.id, code="cancelled"))
        await rig.send(AnswerDelta(id=second.id, text="second answer"))
        await rig.send(AnswerDone(id=second.id, sources=("model",), role="fast_vlm"))
        await h.until(lambda: rig.ended)
        assert rig.events == [
            ("delta", "first answer"),
            ("delta", "second answer"),
            ("done", (("model",), "fast_vlm")),
        ]


async def test_a_question_replaced_while_looking_is_never_sent(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win())
        rig.eyes.eye.gate = asyncio.Event()
        first = asyncio.ensure_future(rig.asker.ask("first?"))
        await h.until(rig.eyes.eye.captures)
        second = asyncio.ensure_future(rig.asker.ask("second?"))
        await h.settle()
        rig.eyes.eye.gate.set()
        await asyncio.gather(first, second)
        ask = await rig.asked()
        await h.settle()
        assert [a.text for a in rig.brain.of_type(Ask)] == ["second?"]
        assert len(rig.images()) == 2  # the first look's frame went nowhere
        assert rig.events == [] and ask.can_see


async def test_cancel_ends_the_open_question_once(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win())
        rig.asker.cancel()  # nothing open: nothing happens
        assert rig.events == []
        await rig.asker.ask(QUESTION)
        ask = await rig.asked()
        rig.asker.cancel()
        rig.asker.cancel()
        assert rig.events == [("error", "cancelled")] and not rig.asker.busy
        await h.until(lambda: rig.brain.of_type(Cancel))
        assert [c.id for c in rig.brain.of_type(Cancel)] == [ask.id]
        await rig.send(AnswerDelta(id=ask.id, text="late"))
        await h.settle()
        assert rig.events == [("error", "cancelled")]


async def test_cancel_while_looking_sends_nothing(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win())
        rig.eyes.eye.gate = asyncio.Event()
        asking = asyncio.ensure_future(rig.asker.ask(QUESTION))
        await h.until(rig.eyes.eye.captures)
        rig.asker.cancel()
        assert rig.events == [("error", "cancelled")]
        rig.eyes.eye.gate.set()
        await asking
        await h.settle()
        assert rig.brain.received == [] and rig.events == [("error", "cancelled")]


async def test_an_answer_that_will_not_stop_is_cut_off(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asker_mod, "MAX_ANSWER_CHARS", 25)
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win())
        await rig.asker.ask(QUESTION)
        ask = await rig.asked()
        for _ in range(4):
            await rig.send(AnswerDelta(id=ask.id, text="ten chars."))
        await h.until(lambda: rig.ended)
        await h.until(lambda: rig.brain.of_type(Cancel))
        assert rig.events == [
            ("delta", "ten chars."),
            ("delta", "ten chars."),
            ("error", "too_large"),
        ]


# ---- questions that cannot be sent -------------------------------------------------------


async def test_with_the_brain_down_nothing_is_looked_at(rig: Rig) -> None:
    async with rig:
        await rig.eyes.focus(h.win())
        await rig.asker.ask(QUESTION)
        assert rig.events == [("error", "brain_down")]
        assert rig.eyes.started == 0 and rig.eyes.eye.captures() == []
        assert not rig.asker.busy


@pytest.mark.parametrize(
    ("text", "code"),
    [("", "bad_request"), (" \n\t ", "bad_request"), ("x" * 4001, "too_large")],
)
async def test_an_empty_or_overlong_question_is_refused_before_looking(
    rig: Rig, text: str, code: str
) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win())
        await rig.asker.ask(text)
        assert rig.events == [("error", code)]
        assert rig.eyes.eye.captures() == [] and rig.brain.received == []


async def test_a_question_that_is_not_valid_text_is_refused_without_a_trace(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win(caption=f"{CANARY} title"))
        with h.captured_logs() as logs:
            await rig.asker.ask(f"{CANARY} \ud800 lone surrogate")
        assert rig.events == [("error", "bad_request")]
        await h.settle()
        assert rig.brain.of_type(Ask) == [] and rig.images() == []
        assert any("ask_rejected" in line for line in logs)
        assert CANARY not in "".join(logs)


class StubLink:
    """A link that fails in a chosen way; ``state`` says up."""

    def __init__(self, failure: Callable[[str], Exception] | None = None) -> None:
        self.failure = failure
        self.state = LinkState(
            "up", models=(ModelStatus(role="smart_model", model="m", available=True, loaded=True),)
        )
        self.prewarmed: list[Role] = []
        self.cancelled: list[str] = []
        self.asks = 0

    def ask(
        self,
        text: str,
        *,
        app: AppContext | None,
        window: WindowContext | None,
        images: Sequence[Image] = (),
        can_see: bool,
        blind_reason: str | None,
        on_delta: Callable[[str], None],
        on_done: Callable[[AnswerDone], None],
        on_error: Callable[[str], None],
    ) -> str:
        self.asks += 1
        if self.failure is not None:
            raise self.failure(text)
        return f"r{self.asks}"

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)

    def prewarm(self, role: Role) -> None:
        self.prewarmed.append(role)


def stub_asker(
    link: StubLink, eyes: h.SensesRig, events: list[tuple[str, object]]
) -> AskController:
    return AskController(
        link=link,
        senses=eyes.senses,
        identity=lambda: Current(APP, None, False, ()),
        on_delta=lambda text: events.append(("delta", text)),
        on_done=lambda sources, role: events.append(("done", (sources, role))),
        on_error=lambda code: events.append(("error", code)),
    )


def _validation_error(text: str) -> Exception:
    try:
        Ask(id=text, text=text)
    except ValueError as exc:  # a pydantic ValidationError, which quotes the question
        assert CANARY in str(exc)
        return exc
    raise AssertionError("expected a validation error")


@pytest.mark.parametrize(
    ("failure", "code", "logged"),
    [
        (lambda _text: BrainDownError(), "brain_down", "ask_failed"),
        (_validation_error, "bad_request", '"kind":"ValidationError"'),
        (lambda text: ValueError(text), "bad_request", '"kind":"ValueError"'),
    ],
)
async def test_link_failures_become_codes_and_their_text_is_never_logged(
    failure: Callable[[str], Exception], code: str, logged: str
) -> None:
    eyes = h.SensesRig()
    await eyes.focus(h.win(caption=f"{CANARY} title"))
    events: list[tuple[str, object]] = []
    asker = stub_asker(StubLink(failure), eyes, events)
    with h.captured_logs() as logs:
        await asker.ask(QUESTION)
    assert events == [("error", code)] and not asker.busy
    assert logged in "".join(logs)  # the kind of failure, never its text
    assert CANARY not in "".join(logs)


async def test_nothing_about_a_normal_question_reaches_the_logs(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.eyes.focus(h.win(caption=f"{CANARY} title"))
        with h.captured_logs() as logs:
            await rig.asker.ask(QUESTION)
            ask = await rig.asked()
            await rig.send(AnswerDelta(id=ask.id, text=f"{CANARY} answer"))
            await rig.send(AnswerDone(id=ask.id, sources=("screen",), role="smart_model"))
            await h.until(lambda: rig.ended)
        text = "".join(logs)
        assert "ask_sent" in text and "ask_done" in text
        assert CANARY not in text and "Good App" not in text and "GoodApp" not in text


async def test_a_failing_look_does_not_lose_the_question() -> None:
    class BrokenSenses:
        def __init__(self, inner: h.SensesRig) -> None:
            self._inner = inner

        @property
        def state(self) -> object:
            return self._inner.senses.state

        async def look_once(self, trigger: object = None) -> object:
            raise RuntimeError(CANARY)

    eyes = h.SensesRig()
    await eyes.focus(h.win())
    link = StubLink()
    events: list[tuple[str, object]] = []
    asker = AskController(
        link=link,
        senses=BrokenSenses(eyes),  # type: ignore[arg-type]
        identity=lambda: Current(APP, None, False, ()),
        on_delta=lambda text: events.append(("delta", text)),
        on_done=lambda sources, role: events.append(("done", (sources, role))),
        on_error=lambda code: events.append(("error", code)),
    )
    with h.captured_logs() as logs:
        await asker.ask(QUESTION)
    assert link.asks == 1 and events == [] and asker.busy
    assert CANARY not in "".join(logs)


# ---- prewarm -----------------------------------------------------------------------------


def _models(*roles: tuple[Role, bool]) -> tuple[ModelStatus, ...]:
    return tuple(
        ModelStatus(role=role, model="m", available=there, loaded=False) for role, there in roles
    )


@pytest.mark.parametrize(
    ("models", "role"),
    [
        (_models(("smart_model", True), ("fast_vlm", True)), "smart_model"),
        (_models(("smart_model", False), ("fast_vlm", True)), "fast_vlm"),
        (_models(("fast_vlm", True)), "fast_vlm"),
        (_models(("smart_model", False), ("fast_vlm", False)), "smart_model"),
        ((), "smart_model"),  # nothing known yet: the role that normally answers
    ],
)
async def test_prewarm_asks_for_the_role_that_will_answer(
    models: tuple[ModelStatus, ...], role: str
) -> None:
    link = StubLink()
    link.state = LinkState("up", models=models)
    asker = stub_asker(link, h.SensesRig(), [])
    asker.prewarm()
    assert link.prewarmed == [role]


async def test_prewarm_while_down_does_nothing_and_reaches_the_brain_when_up(rig: Rig) -> None:
    async with rig:
        rig.asker.prewarm()
        await rig.up()
        rig.asker.prewarm()
        await h.until(lambda: rig.brain.of_type(Prewarm))
        assert [p.role for p in rig.brain.of_type(Prewarm)] == ["smart_model"]
