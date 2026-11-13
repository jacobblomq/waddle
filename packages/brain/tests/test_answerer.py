"""QaAnswerer: role choice, the one host tool call, streaming, cleaning, caps and timings."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import BaseModel

from waddle_brain.config import BrainConfig, LimitsConfig, RoleConfig
from waddle_brain.contracts import (
    Answerer,
    AnswerResult,
    AskContext,
    ChatMessage,
    ProviderError,
    ToolUnavailableError,
)
from waddle_brain.qa import QaAnswerer, build_answerer
from waddle_brain.qa.answerer import StreamCleaner
from waddle_brain.qa.prompt import SYSTEM_PREFIX
from waddle_core.link import AppContext, ErrorCode, ModelStatus, Role, WindowContext
from waddle_core.tools import NoArgs, ToolArgs, ToolName
from waddle_core.untrusted import clean

BLENDER = AppContext(name="Blender", version="5.3.1", packaging="flatpak")
FIXTURES: list[dict[str, str]] = json.loads(
    (Path(__file__).parent / "injection_fixtures.json").read_text(encoding="utf-8")
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Provider:
    """A minimal scripted model: yields ``pieces``, then fails with ``error`` if set."""

    def __init__(
        self,
        pieces: Sequence[str] = ("Press Ctrl+B.",),
        *,
        error: ErrorCode | None = None,
        clock: Clock | None = None,
        step_s: float = 0.0,
        hang_after: int | None = None,
    ) -> None:
        self.pieces = pieces
        self.error = error
        self.clock = clock
        self.step_s = step_s
        self.hang_after = hang_after
        self.chats: list[tuple[Role, tuple[ChatMessage, ...]]] = []
        self.loads: list[Role] = []
        self.load_error: ErrorCode | None = None
        self.yielded = 0
        self.stream_closed = False

    async def chat(
        self,
        role: Role,
        messages: Sequence[ChatMessage],
        *,
        schema: type[BaseModel] | None = None,
    ) -> AsyncIterator[str]:
        assert schema is None
        self.chats.append((role, tuple(messages)))
        try:
            for index, piece in enumerate(self.pieces):
                if self.hang_after == index:
                    await asyncio.Event().wait()
                if self.clock:
                    self.clock.now += self.step_s
                self.yielded += 1
                yield piece
            if self.error:
                raise ProviderError(self.error, waking=True)
        finally:
            self.stream_closed = True

    async def load(self, role: Role) -> None:
        self.loads.append(role)
        if self.load_error:
            raise ProviderError(self.load_error)

    async def unload(self, role: Role) -> None: ...

    async def status(self) -> list[ModelStatus]:
        return [ModelStatus(role="smart_model", model="test-model", available=True, loaded=False)]

    async def close(self) -> None: ...


class Tools:
    def __init__(
        self,
        result: Mapping[str, object] | None = None,
        *,
        clock: Clock | None = None,
        took_s: float = 0.0,
    ) -> None:
        self.result = result
        self.calls: list[tuple[ToolName, ToolArgs]] = []
        self.clock = clock
        self.took_s = took_s

    async def call(self, tool: ToolName, args: ToolArgs) -> Mapping[str, object]:
        self.calls.append((tool, args))
        if self.clock:
            self.clock.now += self.took_s
        if self.result is None:
            raise ToolUnavailableError("SECRET-HOST-CODE")
        return self.result


class Sink:
    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.got_one = asyncio.Event()

    async def delta(self, text: str) -> None:
        self.deltas.append(text)
        self.got_one.set()

    @property
    def text(self) -> str:
        return "".join(self.deltas)


def make_config(
    *,
    smart_vision: bool | None = True,
    fast_vision: bool | None = True,
    max_chars: int = 6000,
    smart_ctx: int = 8192,
    fast_ctx: int = 8192,
) -> BrainConfig:
    """``None`` for a role leaves it out of the config."""
    roles: dict[Role, RoleConfig] = {"embedder": RoleConfig(model="test-embedder")}
    if smart_vision is not None:
        roles["smart_model"] = RoleConfig(
            model="test-smart", vision=smart_vision, num_ctx=smart_ctx
        )
    if fast_vision is not None:
        roles["fast_vlm"] = RoleConfig(model="test-fast", vision=fast_vision, num_ctx=fast_ctx)
    return BrainConfig(roles=roles, limits=LimitsConfig(max_answer_chars=max_chars))


def make_ask(
    *,
    app: AppContext | None = BLENDER,
    images: Mapping[str, bytes] | None = None,
    can_see: bool = True,
    title: str = "",
    text: str = "How do I bevel?",
) -> AskContext:
    return AskContext(
        request_id="r1",
        text=text,
        app=app,
        window=WindowContext(title=title),
        images=images or {},
        can_see=can_see,
        blind_reason=None if can_see else "locked",
    )


def run(
    provider: Provider,
    ask: AskContext,
    *,
    config: BrainConfig | None = None,
    tools: Tools | None = None,
    clock: Clock | None = None,
) -> tuple[AnswerResult, Sink, Tools]:
    tools = tools or Tools({"mode": "EDIT"})
    answerer = QaAnswerer(config or make_config(), provider, clock=clock or Clock())

    async def go() -> tuple[AnswerResult, Sink]:
        sink = Sink()
        return await answerer.answer(ask, tools, sink), sink

    result, sink = asyncio.run(go())
    return result, sink, tools


SHOT = {"overview": b"overview-jpeg", "crop": b"crop-jpeg"}


# ---- wiring ------------------------------------------------------------------------------


def test_build_answerer_returns_a_working_answerer() -> None:
    provider = Provider()
    answerer: Answerer = build_answerer(make_config(), provider)
    assert isinstance(answerer, QaAnswerer)
    assert asyncio.run(answerer.status()) == asyncio.run(provider.status())


def test_the_prompt_sent_to_the_model_is_the_assembled_one() -> None:
    provider = Provider()
    run(provider, make_ask(title="untitled.blend", images=SHOT))

    ((_, messages),) = provider.chats
    assert [m.role for m in messages] == ["system", "user"]
    assert messages[0].content == SYSTEM_PREFIX
    assert "version: 5.3.1" in messages[1].content
    assert "<<<window_title\nuntitled.blend\n>>>" in messages[1].content
    assert messages[1].content.endswith("[Question]\nHow do I bevel?")
    assert messages[1].images == (b"overview-jpeg", b"crop-jpeg")  # overview first


# ---- role choice -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("smart", "fast", "has_image", "role", "sent"),
    [
        (True, True, True, "smart_model", True),
        (True, False, True, "smart_model", True),
        (False, True, True, "fast_vlm", True),
        (False, False, True, "smart_model", False),
        (False, None, True, "smart_model", False),
        (True, True, False, "smart_model", False),
        (False, True, False, "smart_model", False),
        (None, True, True, "fast_vlm", True),
        (None, True, False, "fast_vlm", False),
        (None, False, True, "fast_vlm", False),
    ],
)
def test_role_choice(
    smart: bool | None, fast: bool | None, has_image: bool, role: Role, sent: bool
) -> None:
    provider = Provider()
    config = make_config(smart_vision=smart, fast_vision=fast)
    result, _, _ = run(provider, make_ask(images=SHOT if has_image else None), config=config)

    assert result.role == role
    assert provider.chats[0][0] == role
    user = provider.chats[0][1][1]
    assert bool(user.images) == sent
    assert ("screen" in result.sources) == sent
    assert ("No screenshot" in user.content) == (not sent)


def test_no_chat_role_configured_is_model_missing() -> None:
    with pytest.raises(ProviderError) as caught:
        run(Provider(), make_ask(), config=make_config(smart_vision=None, fast_vision=None))
    assert caught.value.code == "model_missing"


def test_images_are_not_sent_when_the_host_says_it_cannot_see() -> None:
    provider = Provider()
    result, _, _ = run(provider, make_ask(images=SHOT, can_see=False))

    user = provider.chats[0][1][1]
    assert user.images == ()
    assert "the screen is locked" in user.content
    assert "screen" not in result.sources


# ---- the host tool -----------------------------------------------------------------------


def test_app_state_is_fetched_once_when_the_app_is_missing() -> None:
    provider = Provider()
    result, _, tools = run(provider, make_ask(app=None), tools=Tools({"mode": "SCULPT"}))

    assert tools.calls == [(ToolName.GET_APP_STATE, NoArgs())]
    assert '<<<app_state\n{"mode": "SCULPT"}\n>>>' in provider.chats[0][1][1].content
    assert result.sources == ("system", "model")


def test_no_tool_call_when_the_app_is_known() -> None:
    provider = Provider()
    result, _, tools = run(provider, make_ask())

    assert tools.calls == []
    assert "<<<" not in provider.chats[0][1][1].content
    assert result.sources == ("app", "model")


def test_tool_unavailable_carries_on_without_it(logs: io.StringIO) -> None:
    provider = Provider()
    result, sink, tools = run(provider, make_ask(app=None), tools=Tools(None))

    assert len(tools.calls) == 1
    assert sink.text == "Press Ctrl+B."
    assert "app_state" not in provider.chats[0][1][1].content
    assert result.sources == ("model",)
    assert "tool_unavailable" in logs.getvalue()
    assert "SECRET-HOST-CODE" not in logs.getvalue()


def test_an_empty_tool_result_is_not_a_source() -> None:
    result, _, _ = run(Provider(), make_ask(app=None), tools=Tools({}))
    assert result.sources == ("model",)


def test_a_tool_result_that_did_not_fit_the_prompt_is_not_a_source() -> None:
    # 1024 tokens of context cannot hold the fixed parts and a long question as well.
    provider = Provider()
    ask = make_ask(app=None, text="Why? " * 600)
    result, _, tools = run(provider, ask, config=make_config(smart_ctx=1024))

    assert len(tools.calls) == 1
    assert "<<<" not in provider.chats[0][1][1].content
    assert result.sources == ("model",)


def test_the_budget_is_the_answering_roles_context_less_its_images() -> None:
    # The question fits fast_vlm's context with room for the tool result, but not once two
    # images are counted; smart_model's far larger context must play no part.
    config = make_config(smart_vision=False, fast_vision=True, smart_ctx=65536, fast_ctx=4096)
    ask = make_ask(app=None, text="Why? " * 500, images=SHOT)
    with_images, _, _ = run(Provider(), ask, config=config)
    assert with_images.role == "fast_vlm"
    assert with_images.sources == ("screen", "model")

    small = make_config(smart_vision=False, fast_vision=True, smart_ctx=4096, fast_ctx=65536)
    without, _, _ = run(Provider(), make_ask(app=None, text="Why? " * 500), config=small)
    assert without.role == "smart_model"
    assert without.sources == ("system", "model")


def test_a_question_with_nothing_printable_is_refused_before_any_work() -> None:
    provider = Provider()
    answerer = QaAnswerer(make_config(), provider)
    tools = Tools({"mode": "EDIT"})

    with pytest.raises(ProviderError) as caught:
        asyncio.run(answerer.answer(make_ask(app=None, text=" \u200b\n\x00"), tools, Sink()))

    assert caught.value.code == "bad_request"
    assert tools.calls == []
    assert provider.chats == []


# ---- sources -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("app", "image", "tool", "sources"),
    [
        (BLENDER, True, True, ("screen", "app", "model")),
        (BLENDER, False, True, ("app", "model")),
        (None, True, True, ("screen", "system", "model")),
        (None, True, False, ("screen", "model")),
        (None, False, False, ("model",)),
        (AppContext(name="", version="1.0"), False, True, ("model",)),  # unnamed: not "app"
    ],
)
def test_sources(app: AppContext | None, image: bool, tool: bool, sources: tuple[str, ...]) -> None:
    tools = Tools({"mode": "EDIT"} if tool else None)
    result, _, _ = run(Provider(), make_ask(app=app, images=SHOT if image else None), tools=tools)
    assert result.sources == sources


# ---- timings -----------------------------------------------------------------------------


def test_timings_come_from_the_injected_clock() -> None:
    clock = Clock()
    provider = Provider(["\u200b", "One ", "two ", "three."], clock=clock, step_s=0.25)
    tools = Tools({"mode": "EDIT"}, clock=clock, took_s=0.1)
    result, sink, _ = run(provider, make_ask(app=None), tools=tools, clock=clock)

    assert sink.deltas == ["One ", "two ", "three."]
    # 0.1 s tool + two model steps: the first piece held nothing printable.
    assert result.timings.first_delta_ms == 600
    assert result.timings.total_ms == 1100
    assert result.timings.model_load_ms is None


# ---- output hygiene ----------------------------------------------------------------------

DIRTY = (
    "  \n",
    "Open the ",
    "\u202e",  # a bidi override alone in its piece
    "Edit\u200b",
    "\u200b",  # an invisible character as the only content
    " menu\x1b",
    "[31m,\u2028then ",
    "\ufeff\u2066Pre",
    "ferences\u2069.\x00",
)


def test_control_bidi_and_invisible_characters_never_reach_the_sink() -> None:
    _, sink, _ = run(Provider(DIRTY), make_ask())

    assert sink.text == "Open the Edit menu[31m,\nthen Preferences."
    assert sink.deltas == ["Open the ", "Edit", " menu", "[31m,\nthen ", "Pre", "ferences."]
    assert all(sink.deltas)  # an empty delta is not a valid link message


def test_stream_cleaning_matches_clean_wherever_the_pieces_are_split() -> None:
    whole = "".join(f["text"] for f in FIXTURES)[:3000] + "".join(DIRTY)
    expected = clean(whole, len(whole)).lstrip()
    for size in (1, 2, 3, 7, 64, len(whole)):
        cleaner = StreamCleaner(len(whole) + 2)
        pieces = [whole[i : i + size] for i in range(0, len(whole), size)]
        assert "".join(cleaner.feed(piece) for piece in pieces) == expected
        assert not cleaner.cut
        assert cleaner.chars == len(expected)


def test_the_answer_stops_at_the_cap_and_the_model_call_is_closed() -> None:
    provider = Provider(["abcdefghij"] * 100)
    answerer = QaAnswerer(make_config(max_chars=205), provider)

    async def go() -> Sink:
        sink = Sink()
        await answerer.answer(make_ask(), Tools(), sink)
        assert provider.stream_closed  # closed by the answerer, not left to the event loop
        return sink

    sink = asyncio.run(go())
    assert len(sink.text) == 205
    assert sink.text == ("abcdefghij" * 21)[:204] + "…"
    assert provider.yielded == 21  # nothing was read after the cut


def test_an_answer_shorter_than_the_cap_is_not_marked_as_cut() -> None:
    _, sink, _ = run(Provider(["a" * 100, "b" * 99]), make_ask(), config=make_config(max_chars=200))
    assert sink.text == "a" * 100 + "b" * 99


def test_characters_removed_by_cleaning_do_not_count_towards_the_cap() -> None:
    provider = Provider(["\u200b" * 500, "ok"])
    _, sink, _ = run(provider, make_ask(), config=make_config(max_chars=200))
    assert sink.text == "ok"


@pytest.mark.parametrize("pieces", [(), ("",), ("  \n", "\t"), ("\u200b\u202e", "\x00")])
def test_no_printable_output_is_bad_model_output(pieces: tuple[str, ...]) -> None:
    provider = Provider(pieces)
    answerer = QaAnswerer(make_config(), provider)
    sink = Sink()

    with pytest.raises(ProviderError) as caught:
        asyncio.run(answerer.answer(make_ask(), Tools(), sink))

    assert caught.value.code == "bad_model_output"
    assert sink.deltas == []


def test_provider_errors_pass_through_after_partial_output() -> None:
    provider = Provider(["Half an ans"], error="model_timeout")
    answerer = QaAnswerer(make_config(), provider)
    sink = Sink()

    with pytest.raises(ProviderError) as caught:
        asyncio.run(answerer.answer(make_ask(), Tools(), sink))

    assert caught.value.code == "model_timeout"
    assert caught.value.waking
    assert sink.text == "Half an ans"


def test_cancelling_stops_the_stream_and_raises_cancelled() -> None:
    provider = Provider(["First words. ", "never sent"], hang_after=1)
    answerer = QaAnswerer(make_config(), provider)

    async def go() -> Sink:
        sink = Sink()
        task = asyncio.create_task(answerer.answer(make_ask(), Tools(), sink))
        await sink.got_one.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return sink

    sink = asyncio.run(go())
    assert sink.deltas == ["First words. "]
    assert provider.stream_closed


def test_a_cancel_from_the_sink_is_not_swallowed() -> None:
    class CancellingSink(Sink):
        async def delta(self, text: str) -> None:
            raise asyncio.CancelledError

    provider = Provider(["a", "b"])
    answerer = QaAnswerer(make_config(), provider)

    async def go() -> None:
        with pytest.raises(asyncio.CancelledError):
            await answerer.answer(make_ask(), Tools(), CancellingSink())

    asyncio.run(go())
    assert provider.stream_closed


# ---- logging, prewarm, status ------------------------------------------------------------


def test_logs_hold_no_question_answer_title_or_tool_text(logs: io.StringIO) -> None:
    provider = Provider(["CANARY-ANSWER"])
    ask = AskContext(
        request_id="r1",
        text="CANARY-QUESTION",
        app=None,
        window=WindowContext(title="CANARY-TITLE", resource_class="CANARY-CLASS"),
        images=SHOT,
        can_see=True,
    )
    run(provider, ask, tools=Tools({"CANARY-KEY": "CANARY-VALUE"}))

    logged = logs.getvalue()
    assert "answer_streamed" in logged
    assert "CANARY" not in logged
    assert "test-smart" not in logged


def test_prewarm_loads_the_role() -> None:
    provider = Provider()
    asyncio.run(QaAnswerer(make_config(), provider).prewarm("smart_model"))
    assert provider.loads == ["smart_model"]


def test_prewarm_swallows_provider_errors_and_logs_the_code(logs: io.StringIO) -> None:
    provider = Provider()
    provider.load_error = "model_unreachable"

    asyncio.run(QaAnswerer(make_config(), provider).prewarm("fast_vlm"))

    assert provider.loads == ["fast_vlm"]
    assert "prewarm_failed" in logs.getvalue()
    assert "model_unreachable" in logs.getvalue()
