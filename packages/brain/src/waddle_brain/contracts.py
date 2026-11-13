"""Interfaces between the brain's parts (ARCHITECTURE §5.2, §5.3).

The link server, the model layer and the question answerer are built against these, so each
can be tested with fakes of the other two.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel

from waddle_core.link import AppContext, ErrorCode, ModelStatus, Role, Timings, WindowContext
from waddle_core.tools import ToolArgs, ToolName

Source = Literal["screen", "system", "app", "model"]


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str
    images: tuple[bytes, ...] = ()  # encoded JPEG/PNG, only on user messages


class ProviderError(Exception):
    """A model call failed in a way the UI can describe honestly."""

    def __init__(self, code: ErrorCode, *, waking: bool = False):
        super().__init__(code)
        self.code: ErrorCode = code
        self.waking = waking


class ModelProvider(Protocol):
    """A local model server. Model names come from config; none appear in code."""

    def chat(
        self,
        role: Role,
        messages: Sequence[ChatMessage],
        *,
        schema: type[BaseModel] | None = None,
    ) -> AsyncIterator[str]:
        """Stream the reply as text pieces. With ``schema``, decoding is constrained to it
        and the concatenated pieces are one JSON document. Raises ``ProviderError``."""
        ...

    async def load(self, role: Role) -> None: ...
    async def unload(self, role: Role) -> None: ...
    async def status(self) -> list[ModelStatus]: ...
    async def close(self) -> None: ...


class HostTools(Protocol):
    """The host's closed tool list, scoped to one open request and capped per request."""

    async def call(self, tool: ToolName, args: ToolArgs) -> Mapping[str, object]:
        """Returns the host's result (untrusted data). Raises ``ToolUnavailableError``."""
        ...


class ToolUnavailableError(Exception):
    """The host refused, the cap was reached, or the request is no longer open."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AnswerSink(Protocol):
    async def delta(self, text: str) -> None: ...


@dataclass(frozen=True)
class AskContext:
    request_id: str
    text: str  # typed by the user
    app: AppContext | None
    window: WindowContext | None
    images: Mapping[str, bytes] = field(default_factory=dict)  # kind -> encoded image
    can_see: bool = False
    blind_reason: str | None = None


@dataclass(frozen=True)
class AnswerResult:
    sources: tuple[Source, ...] = ()
    role: Role | None = None
    timings: Timings = field(default_factory=Timings)


class Answerer(Protocol):
    async def answer(self, ask: AskContext, tools: HostTools, sink: AnswerSink) -> AnswerResult:
        """Answer one question, streaming text into ``sink``. Raises ``ProviderError``.
        Must stop promptly when cancelled (``asyncio.CancelledError``)."""
        ...

    async def prewarm(self, role: Role) -> None: ...
    async def status(self) -> list[ModelStatus]: ...
