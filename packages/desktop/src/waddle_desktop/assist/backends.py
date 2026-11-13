"""What answers the brain's four host tools (ARCHITECTURE §5.2, D-042).

``HostToolDispatcher`` has already checked that the call names one of the tools, that its
arguments pass the schema and that it belongs to an open question of yours. This module
produces the answers, and keeps three promises of its own:

- ``app_state`` passes the privacy gate: for a locked screen, a pause or a blocked window it
  says only that Waddle cannot see and why. A window title leaves only for a window Waddle
  may look at, cleaned and capped.
- ``man`` and ``tldr`` text comes from the sandbox, is cleaned, and is cut so that the whole
  result fits ``MAX_TOOL_RESULT_BYTES`` the way the dispatcher counts it.
- Failures are short codes (``ToolError``); no exception text and no command output reaches
  the logs.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Final

from waddle_core.tools import MAX_TOOL_CALLS_PER_REQUEST, MAX_TOOL_RESULT_BYTES, ToolError
from waddle_core.untrusted import ELLIPSIS, clean
from waddle_desktop.assist import sight
from waddle_desktop.assist.identity_service import Current
from waddle_desktop.assist.shared_exec import SharedExec
from waddle_desktop.sandbox.readonly_exec import ExecResult, Reason
from waddle_desktop.sensors.senses import EyesState
from waddle_desktop.telemetry.basic import system_stats

NOT_FOUND: Final = "not_found"
UNAVAILABLE: Final = "unavailable"
TIMEOUT: Final = "timeout"
FAILED: Final = "failed"
MAX_DOC_CHARS: Final = 32_000  # more than the sandbox's own byte caps allow through
# System numbers are read on a worker thread (``statvfs`` on a dead network mount can hang,
# and the I/O loop also carries the privacy latch). At most this many readers at a time, so
# readers that never come back cannot use up the thread pool.
MAX_STATS_READERS: Final = MAX_TOOL_CALLS_PER_REQUEST

SystemStats = Callable[[str], dict[str, object]]


def result_bytes(data: dict[str, object]) -> int:
    """The size of a tool result exactly as ``HostToolDispatcher.handle`` measures it."""
    text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return len(text.encode())


def fit_text(
    data: dict[str, object], text: str, *, limit: int = MAX_TOOL_RESULT_BYTES
) -> dict[str, object]:
    """``data`` plus ``text`` and ``truncated``, cut so the result is at most ``limit`` bytes.

    Bytes are counted after JSON escaping, where a newline or a quote costs two bytes and
    most non-ASCII characters two to four, so the cut is found by measuring, not guessed.
    """

    def build(body: str, cut: bool) -> dict[str, object]:
        return {**data, "text": body, "truncated": cut}

    whole = build(text, False)
    if result_bytes(whole) <= limit:
        return whole
    low, high = 0, len(text)  # the longest prefix that still fits, by bisection
    while low < high:
        middle = (low + high + 1) // 2
        if result_bytes(build(text[:middle] + ELLIPSIS, True)) <= limit:
            low = middle
        else:
            high = middle - 1
    return build(text[:low] + ELLIPSIS if low else "", True)


class HostBackends:
    """Implements ``waddle_desktop.link.tools_host.ToolBackends``. Lives on the I/O loop."""

    def __init__(
        self,
        *,
        eyes: Callable[[], EyesState],
        identity: Callable[[], Current],
        sandbox: SharedExec,
        helper_installed: Callable[[], bool],
        stats: SystemStats = system_stats,  # type: ignore[assignment]
    ):
        """``eyes`` is the senses' current state and ``identity`` is
        ``IdentityService.current``. ``helper_installed`` says whether the capture helper
        is there to be started (a file check; it starts nothing)."""
        self._eyes = eyes
        self._identity = identity
        self._sandbox = sandbox
        self._helper_installed = helper_installed
        self._stats = stats
        self._stats_running = 0

    async def app_state(self) -> dict[str, object]:
        state = self._eyes()
        blind = sight.gate_reason(state)
        if blind is not None:
            # Locked, paused, blocked, Waddle's own window or none: nothing about it leaves.
            return {"can_see": False, "reason": blind}
        can_capture, _code = sight.capture_capability(
            state, helper_installed=self._helper_installed()
        )
        out: dict[str, object] = {"can_see": can_capture}
        if not can_capture:
            out["reason"] = sight.HELPER_MISSING
        app = self._identity().app
        if app is not None:
            out["app"] = app.name
            out["version"] = app.version if app.version is not None else "unknown"
            out["packaging"] = app.packaging
            out["channel"] = app.channel
            if app.edition:
                out["edition"] = app.edition
        window = sight.window_context(state)
        if window is not None:
            out["window_class"] = window.resource_class
            if window.title:
                out["window_title"] = window.title
        return out

    async def system_stats(self, topic: str) -> dict[str, object]:
        if self._stats_running >= MAX_STATS_READERS:
            raise ToolError(UNAVAILABLE)
        reader = asyncio.get_running_loop().run_in_executor(None, self._stats, topic)
        self._stats_running += 1
        reader.add_done_callback(self._stats_read)
        # Shielded: if the dispatcher stops waiting, the reader still counts until it ends.
        stats = await asyncio.shield(reader)
        if not isinstance(stats, dict):
            raise ToolError(FAILED)
        return stats

    def _stats_read(self, reader: asyncio.Future[dict[str, object]]) -> None:
        # ``shield`` looks at a late error itself, so the loop never reports it (with its text).
        self._stats_running -= 1

    async def man(self, name: str, section: str | None) -> dict[str, object]:
        sandbox = await self._sandbox.ready()
        if sandbox is None:
            raise ToolError(UNAVAILABLE)
        result = await sandbox.man(name, section)
        data: dict[str, object] = {"page": name}
        if section is not None:
            data["section"] = section
        return _doc(data, result)

    async def tldr(self, name: str) -> dict[str, object]:
        sandbox = await self._sandbox.ready()
        if sandbox is None:
            raise ToolError(UNAVAILABLE)
        return _doc({"page": name}, await sandbox.tldr(name))


def _doc(data: dict[str, object], result: ExecResult) -> dict[str, object]:
    """A man or tldr result as tool data, or a ``ToolError`` with a short code."""
    match result.reason:
        case Reason.OK | Reason.TRUNCATED:
            pass
        case Reason.UNAVAILABLE | Reason.NOT_INSTALLED:
            raise ToolError(UNAVAILABLE)
        case Reason.TIMEOUT:
            raise ToolError(TIMEOUT)
        case Reason.FAILED:
            # The tool ran and gave up: for man and tldr that is "no such page".
            raise ToolError(NOT_FOUND)
        case _:
            raise ToolError(FAILED)
    text = clean(result.text, MAX_DOC_CHARS).strip("\n")
    if not text.strip():
        raise ToolError(NOT_FOUND)
    fitted = fit_text(data, text)
    if result.reason is Reason.TRUNCATED:
        fitted["truncated"] = True
    return fitted
