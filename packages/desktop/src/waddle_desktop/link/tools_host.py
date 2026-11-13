"""The host tool contract, enforced on the host (ARCHITECTURE §5.2, D-042).

The brain reads the internet and runs models; this process is unsandboxed. So a ``tool.call``
is run only if it names one of the four tools, its arguments pass the schema, it belongs to an
open request of ours and that request is under its cap. Everything a backend returns is
size-capped, and nothing a backend raises reaches the brain or the logs as text.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Final, Protocol

from waddle_core.link import ToolCall, ToolResult
from waddle_core.tools import (
    MAX_TOOL_CALLS_PER_REQUEST,
    MAX_TOOL_RESULT_BYTES,
    ManArgs,
    NoArgs,
    SystemStatsArgs,
    TldrArgs,
    ToolArgs,
    ToolError,
    ToolName,
    validate_tool_call,
)
from waddle_desktop import log

BACKEND_TIMEOUT: Final = 10.0
_CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")


class ToolBackends(Protocol):
    """What actually answers each tool. Arguments arrive already validated.

    A backend may raise ``waddle_core.tools.ToolError(code)`` to refuse with a short code of
    its own (for example ``not_found``); any other exception is reported as ``backend_failed``.
    """

    async def app_state(self) -> dict[str, object]: ...
    async def system_stats(self, topic: str) -> dict[str, object]: ...
    async def man(self, name: str, section: str | None) -> dict[str, object]: ...
    async def tldr(self, name: str) -> dict[str, object]: ...


class HostToolDispatcher:
    """Validates one ``tool.call`` and runs it, or says why not."""

    def __init__(self, backends: ToolBackends, *, timeout: float = BACKEND_TIMEOUT):
        self._backends = backends
        self._timeout = timeout

    async def handle(self, request_open: bool, calls_so_far: int, call: ToolCall) -> ToolResult:
        """Answer ``call``. ``calls_so_far`` counts earlier tool calls of the same request."""
        if not request_open:
            return _reject(call, _known(call.tool), "no_open_request")
        if calls_so_far >= MAX_TOOL_CALLS_PER_REQUEST:
            return _reject(call, _known(call.tool), "cap_reached")
        try:
            name, args = validate_tool_call(call.tool, call.args)
        except ToolError as exc:
            return _reject(call, _known(call.tool), exc.code)
        try:
            async with asyncio.timeout(self._timeout):
                data: object = await self._run(args)  # checked below, not assumed
        except TimeoutError:
            return _reject(call, name, "timeout")
        except ToolError as exc:
            code = exc.code if isinstance(exc.code, str) and _CODE.fullmatch(exc.code) else ""
            return _reject(call, name, code or "backend_failed")
        except Exception:  # whatever went wrong, its text stays here
            return _reject(call, name, "backend_failed")
        if not isinstance(data, dict):
            return _reject(call, name, "bad_result")
        try:
            text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            size = len(text.encode())
            if size > MAX_TOOL_RESULT_BYTES:
                return _reject(call, name, "too_large")
            result = ToolResult(id=call.id, call_id=call.call_id, ok=True, data=data)
        except (TypeError, ValueError, RecursionError):  # not JSON, or not valid text
            return _reject(call, name, "bad_result")
        log.event("tool_done", tool=name.value, bytes=size)
        return result

    async def _run(self, args: ToolArgs) -> dict[str, object]:
        match args:
            case SystemStatsArgs():
                return await self._backends.system_stats(args.topic)
            case ManArgs():
                return await self._backends.man(args.name, args.section)
            case TldrArgs():
                return await self._backends.tldr(args.name)
            case NoArgs():
                return await self._backends.app_state()


def _known(tool: str) -> ToolName | None:
    try:
        return ToolName(tool)
    except ValueError:
        return None


def _reject(call: ToolCall, name: ToolName | None, code: str) -> ToolResult:
    # The brain's own tool string is never logged: only a name from our enum, or "unknown".
    log.warning("tool_rejected", tool=name.value if name else "unknown", code=code)
    return ToolResult(id=call.id, call_id=call.call_id, ok=False, error=code)
