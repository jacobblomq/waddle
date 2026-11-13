"""Per-connection and per-ask state for the link server (ARCHITECTURE §5.2).

``ImageStore`` holds the images the host sent ahead of an ask, in memory only, capped in
count and age. ``OpenRequest`` is one ask being answered: it is the answerer's delta sink and
its host-tool channel, and it enforces the answer length cap and the tool caps on the brain
side (the host enforces its own).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Final

from waddle_brain import log
from waddle_brain.contracts import ToolUnavailableError
from waddle_core.link import (
    IMAGE_TTL_SECONDS,
    MAX_DELTA_CHARS,
    MAX_PENDING_IMAGES,
    AnswerDelta,
    AnswerDone,
    AnswerError,
    Pong,
    State,
    ToolCall,
    ToolResult,
    Welcome,
)
from waddle_core.tools import (
    ARGS_FOR,
    MAX_TOOL_CALLS_PER_REQUEST,
    MAX_TOOL_RESULT_BYTES,
    ToolArgs,
    ToolName,
)

TOOL_TIMEOUT_S: Final = 10.0
_HOST_ERROR: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

Outgoing = Welcome | AnswerDelta | AnswerDone | AnswerError | ToolCall | State | Pong
Send = Callable[[Outgoing], Awaitable[None]]


class ImageStore:
    """Images waiting for their ask. Never more than the cap, never older than the TTL."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._items: dict[str, tuple[float, bytes]] = {}  # insertion order = age order

    def __len__(self) -> int:
        return len(self._items)

    def expire(self) -> None:
        cutoff = self._clock() - IMAGE_TTL_SECONDS
        for image_id in [key for key, (at, _) in self._items.items() if at <= cutoff]:
            del self._items[image_id]

    def put(self, image_id: str, data: bytes) -> None:
        """Keep one image; a repeated id replaces the earlier one, the oldest makes room."""
        self.expire()
        self._items.pop(image_id, None)
        while len(self._items) >= MAX_PENDING_IMAGES:
            del self._items[next(iter(self._items))]
        self._items[image_id] = (self._clock(), data)

    def claim(self, image_ids: Sequence[str]) -> list[bytes] | None:
        """Take the named images out of the store, in order. ``None`` if any is missing;
        the ones that were there are dropped all the same, because their ask has failed."""
        self.expire()
        found = [self._items.pop(image_id, None) for image_id in image_ids]
        if any(item is None for item in found):
            return None
        return [item[1] for item in found if item is not None]

    def clear(self) -> None:
        self._items.clear()


class OpenRequest:
    """One ask being answered. Implements ``AnswerSink`` and ``HostTools`` for the answerer.

    ``task`` is the task running the answer; the server sets it right after creating it.
    """

    def __init__(self, request_id: str, send: Send, *, max_chars: int, clock: Callable[[], float]):
        self.id = request_id
        self.open = True
        self.started = False  # the answer task has begun running
        self.full = False  # the answer reached the length cap and was stopped
        self.cancel_requested = False
        self.task: asyncio.Task[None] | None = None
        self.chars = 0
        self.tool_calls = 0
        self.first_delta_at: float | None = None
        self._send = send
        self._max_chars = max_chars
        self._clock = clock
        self._pending: dict[str, asyncio.Future[ToolResult]] = {}

    async def delta(self, text: str) -> None:
        """Forward answer text. At the length cap the rest is dropped and the answer task
        is cancelled; the server then ends the answer normally."""
        if not self.open or self.full or not text:
            return
        # Model output is untrusted: a lone surrogate cannot go on the wire, so it becomes "?".
        text = text.encode("utf-8", "replace").decode("utf-8")
        room = self._max_chars - self.chars
        if len(text) > room:
            text = text[:room]
            self.full = True
        if text and self.first_delta_at is None:
            self.first_delta_at = self._clock()
        self.chars += len(text)
        for start in range(0, len(text), MAX_DELTA_CHARS):
            await self._send(AnswerDelta(id=self.id, text=text[start : start + MAX_DELTA_CHARS]))
        if self.full and self.open and self.task is not None:
            self.task.cancel()

    async def call(self, tool: ToolName, args: ToolArgs) -> Mapping[str, object]:
        """Ask the host to run one tool from its closed list and wait for the result."""
        if not self.open:
            raise ToolUnavailableError("closed")
        if tool not in ARGS_FOR or not isinstance(args, ARGS_FOR[tool]):
            raise ToolUnavailableError("bad_arguments")
        if self.tool_calls >= MAX_TOOL_CALLS_PER_REQUEST:
            raise ToolUnavailableError("too_many_calls")
        self.tool_calls += 1
        call_id = secrets.token_hex(8)
        future: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        try:
            await self._send(
                ToolCall(
                    id=self.id,
                    call_id=call_id,
                    tool=tool.value,
                    args=args.model_dump(exclude_none=True),
                )
            )
            async with asyncio.timeout(TOOL_TIMEOUT_S):
                result = await future
        except TimeoutError:
            log.warning("tool_timeout", tool=tool.value)
            raise ToolUnavailableError("timeout") from None
        finally:
            self._pending.pop(call_id, None)
        if not result.ok:
            # The host's code is a short enum in practice; anything else is not passed on.
            code = result.error or ""
            raise ToolUnavailableError(code if _HOST_ERROR.match(code) else "refused")
        data = result.data or {}
        # Counted the way the host counts it (UTF-8 bytes, not ASCII escapes), so a result
        # the host let through is not refused here.
        size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
        if size > MAX_TOOL_RESULT_BYTES:
            log.warning("tool_result_too_large", tool=tool.value, size=size)
            raise ToolUnavailableError("too_large")
        return data

    def resolve(self, result: ToolResult) -> bool:
        """Hand a host result to the call waiting for it. False if nothing is waiting."""
        future = self._pending.get(result.call_id)
        if not self.open or future is None or future.done():
            return False
        future.set_result(result)
        return True

    def close(self) -> None:
        """The request is over: no more deltas, and waiting tool calls fail."""
        self.open = False
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ToolUnavailableError("closed"))
        self._pending.clear()
