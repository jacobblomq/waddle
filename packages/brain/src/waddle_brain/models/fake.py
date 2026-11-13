"""A scripted ``ModelProvider`` for tests and for running the brain without a model server.

It never touches the network. Replies are queued per role; every call is recorded so a
test can assert what the brain sent.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel

from waddle_brain.contracts import ChatMessage, ProviderError
from waddle_core.link import ErrorCode, ModelStatus, Role

ALL_ROLES: Final[tuple[Role, ...]] = ("fast_vlm", "smart_model", "embedder")
DEFAULT_REPLY: Final = "This is a canned reply from the fake model."


@dataclass(frozen=True)
class FakeReply:
    """One scripted reply: the pieces to stream, then optionally a failure.

    ``error`` with no pieces fails at once; with pieces it fails mid-stream.
    ``delay_s`` is waited before each piece and before the failure.
    """

    pieces: tuple[str, ...] = ()
    delay_s: float = 0.0
    error: ErrorCode | None = None
    waking: bool = False  # passed on with ``error``: the model was still loading


@dataclass(frozen=True)
class FakeCall:
    """A recorded call. ``messages`` and ``schema`` are set for ``chat`` only."""

    method: Literal["chat", "load", "unload", "status", "close"]
    role: Role | None = None
    messages: tuple[ChatMessage, ...] = ()
    schema: type[BaseModel] | None = None


class FakeProvider:
    """Scripted replies per role.

    ``script`` queues replies; each chat takes the next one and the last one repeats.
    A role with no script answers ``DEFAULT_REPLY``. Roles in ``missing`` behave like a
    model that was never pulled.
    """

    def __init__(self, roles: Iterable[Role] = ALL_ROLES) -> None:
        self.roles: tuple[Role, ...] = tuple(roles)
        self.calls: list[FakeCall] = []
        self.missing: set[Role] = set()
        self.loaded: set[Role] = set()
        self.closed = False
        self._scripts: dict[Role, deque[FakeReply]] = {}

    def script(self, role: Role, *replies: FakeReply | str | Sequence[str]) -> None:
        """Queue replies for a role. A string is one piece; a sequence is several."""
        queue = self._scripts.setdefault(role, deque())
        for reply in replies:
            if isinstance(reply, FakeReply):
                queue.append(reply)
            elif isinstance(reply, str):
                queue.append(FakeReply((reply,)))
            else:
                queue.append(FakeReply(tuple(reply)))

    def _next_reply(self, role: Role) -> FakeReply:
        queue = self._scripts.get(role)
        if not queue:
            return FakeReply((DEFAULT_REPLY,))
        return queue.popleft() if len(queue) > 1 else queue[0]

    async def chat(
        self,
        role: Role,
        messages: Sequence[ChatMessage],
        *,
        schema: type[BaseModel] | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(FakeCall("chat", role, tuple(messages), schema))
        self._require(role)
        reply = self._next_reply(role)
        self.loaded.add(role)
        for piece in reply.pieces:
            await asyncio.sleep(reply.delay_s)
            yield piece
        if reply.error is not None:
            await asyncio.sleep(reply.delay_s)
            raise ProviderError(reply.error, waking=reply.waking)

    async def load(self, role: Role) -> None:
        self.calls.append(FakeCall("load", role))
        self._require(role)
        self.loaded.add(role)

    async def unload(self, role: Role) -> None:
        self.calls.append(FakeCall("unload", role))
        self.loaded.discard(role)

    async def status(self) -> list[ModelStatus]:
        self.calls.append(FakeCall("status"))
        return [
            ModelStatus(
                role=role,
                model=f"fake-{role}",
                available=role not in self.missing,
                loaded=role in self.loaded and role not in self.missing,
            )
            for role in self.roles
        ]

    async def close(self) -> None:
        self.calls.append(FakeCall("close"))
        self.closed = True

    def _require(self, role: Role) -> None:
        if role in self.missing or role not in self.roles:
            raise ProviderError("model_missing")
