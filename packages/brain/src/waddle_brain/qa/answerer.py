"""Answer one question by streaming a local model's reply (ARCHITECTURE §5.3, §5.6).

Which model answers, whether images are attached and whether a host tool is called are all
decided here, by code. The model chooses nothing in this phase: it only writes text, and
that text is cleaned before it reaches the UI.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Final

from waddle_brain import log
from waddle_brain.config import BrainConfig
from waddle_brain.contracts import (
    AnswerResult,
    AnswerSink,
    AskContext,
    HostTools,
    ModelProvider,
    ProviderError,
    Source,
    ToolUnavailableError,
)
from waddle_brain.qa.prompt import build_prompt, char_budget
from waddle_core.link import ModelStatus, Role, Timings
from waddle_core.tools import NoArgs, ToolName
from waddle_core.untrusted import ELLIPSIS, clean

_IMAGE_ORDER: Final = ("overview", "crop")


class StreamCleaner:
    """``waddle_core.untrusted.clean`` for text that arrives in pieces.

    ``clean`` judges one character at a time, so cleaning each piece gives the same text as
    cleaning the whole answer, wherever the pieces were split. On top of that this drops
    blank space before the first word and stops at ``limit`` characters in total, ending
    with an ellipsis once the model wants to go past it.
    """

    def __init__(self, limit: int):
        self._room = limit - 1  # one place is kept for the ellipsis
        self._started = False
        self.cut = False
        self.chars = 0

    def feed(self, piece: str) -> str:
        """The text to show for this piece; empty if nothing in it is printable."""
        if self.cut or not piece:
            return ""
        text = clean(piece, len(piece))  # removes or swaps characters, so it never cuts here
        if not self._started:
            text = text.lstrip()
            self._started = bool(text)
        if len(text) > self._room:
            text = text[: self._room] + ELLIPSIS
            self.cut = True
        self._room -= len(text)
        self.chars += len(text)
        return text


class QaAnswerer:
    """Implements ``contracts.Answerer`` on top of a ``ModelProvider``."""

    def __init__(
        self,
        config: BrainConfig,
        provider: ModelProvider,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._provider = provider
        self._clock = clock

    def _choose(self, ask: AskContext) -> tuple[Role, tuple[bytes, ...]]:
        """The role that answers, and the images it is given.

        ``smart_model`` answers. If there is a screenshot it cannot read and ``fast_vlm``
        can, ``fast_vlm`` answers instead; if neither can, the answer is given blind.
        """
        roles = self._config.roles
        smart, fast = roles.get("smart_model"), roles.get("fast_vlm")
        images: tuple[bytes, ...] = ()
        if ask.can_see:
            found = [ask.images[kind] for kind in _IMAGE_ORDER if ask.images.get(kind)]
            images = tuple(found)
        if smart is not None and (smart.vision or not images):
            return "smart_model", images
        if fast is not None and fast.vision and images:
            return "fast_vlm", images
        if smart is not None:
            return "smart_model", ()
        if fast is not None:
            return "fast_vlm", images if fast.vision else ()
        raise ProviderError("model_missing")

    async def _app_state(self, tools: HostTools) -> Mapping[str, object] | None:
        try:
            return await tools.call(ToolName.GET_APP_STATE, NoArgs())
        except ToolUnavailableError:
            log.event("tool_unavailable", tool=ToolName.GET_APP_STATE.value)
            return None

    async def answer(self, ask: AskContext, tools: HostTools, sink: AnswerSink) -> AnswerResult:
        started = self._clock()
        if not clean(ask.text, max(1, len(ask.text))).strip():
            raise ProviderError("bad_request")  # nothing printable was asked
        role, images = self._choose(ask)
        tool_result = await self._app_state(tools) if ask.app is None else None
        prompt = build_prompt(
            ask,
            images=images,
            tool_result=tool_result,
            budget=char_budget(self._config.roles[role].num_ctx, len(images)),
        )
        cleaner = StreamCleaner(self._config.limits.max_answer_chars)
        first_at: float | None = None
        stream = self._provider.chat(role, prompt.messages)
        try:
            async for piece in stream:
                text = cleaner.feed(piece)
                if text:
                    if first_at is None:
                        first_at = self._clock()
                    await sink.delta(text)
                if cleaner.cut:
                    break
        finally:
            # Stop the model call at once on a cut, an error or a cancel.
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        if first_at is None:
            raise ProviderError("bad_model_output")
        sources: list[Source] = []
        if images:
            sources.append("screen")
        if prompt.app_known:
            sources.append("app")
        if "app_state" in prompt.blocks:
            sources.append("system")
        sources.append("model")
        timings = Timings(
            first_delta_ms=_ms(first_at - started), total_ms=_ms(self._clock() - started)
        )
        log.event(
            "answer_streamed",
            role=role,
            images=len(images),
            blocks=len(prompt.blocks),
            chars=cleaner.chars,
            cut=cleaner.cut,
            first_delta_ms=timings.first_delta_ms,
            total_ms=timings.total_ms,
        )
        return AnswerResult(sources=tuple(sources), role=role, timings=timings)

    async def prewarm(self, role: Role) -> None:
        """Load a model ahead of a question. A failure only costs time later, so it is
        logged and not raised."""
        try:
            await self._provider.load(role)
        except ProviderError as exc:
            log.warning("prewarm_failed", role=role, code=exc.code)

    async def status(self) -> list[ModelStatus]:
        return await self._provider.status()


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))
