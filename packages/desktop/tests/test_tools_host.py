"""The host tool contract: what the brain may ask for, and every way it is told no."""

from __future__ import annotations

import asyncio
import logging

import pytest

from waddle_core.link import ToolCall, ToolResult
from waddle_core.tools import MAX_TOOL_CALLS_PER_REQUEST, MAX_TOOL_RESULT_BYTES, ToolError
from waddle_desktop.link.tools_host import HostToolDispatcher

CANARY = "CANARY-7f3a"


class Backends:
    """Records calls; ``fail`` raises, ``hang`` never answers, ``reply`` overrides the data."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.fail: Exception | None = None
        self.hang = False
        self.reply: object = None

    async def _answer(self, *call: object) -> dict[str, object]:
        self.calls.append(call)
        if self.hang:
            await asyncio.Event().wait()
        if self.fail is not None:
            raise self.fail
        return {"tool": call[0]} if self.reply is None else self.reply  # type: ignore[return-value]

    async def app_state(self) -> dict[str, object]:
        return await self._answer("app_state")

    async def system_stats(self, topic: str) -> dict[str, object]:
        return await self._answer("system_stats", topic)

    async def man(self, name: str, section: str | None) -> dict[str, object]:
        return await self._answer("man", name, section)

    async def tldr(self, name: str) -> dict[str, object]:
        return await self._answer("tldr", name)


@pytest.fixture
def backends() -> Backends:
    return Backends()


def _call(tool: str, args: dict[str, object] | None = None) -> ToolCall:
    return ToolCall(id="req1", call_id="call1", tool=tool, args=args or {})


def _handle(
    backends: Backends,
    call: ToolCall,
    *,
    request_open: bool = True,
    calls_so_far: int = 0,
    timeout: float = 10.0,
) -> ToolResult:
    dispatcher = HostToolDispatcher(backends, timeout=timeout)
    return asyncio.run(dispatcher.handle(request_open, calls_so_far, call))


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("get_app_state", {}, ("app_state",)),
        ("get_system_stats", {"topic": "memory"}, ("system_stats", "memory")),
        ("man", {"name": "ls", "section": "1"}, ("man", "ls", "1")),
        ("man", {"name": "ls"}, ("man", "ls", None)),
        ("tldr", {"name": "tar"}, ("tldr", "tar")),
    ],
)
def test_valid_calls_reach_their_backend(
    backends: Backends, tool: str, args: dict[str, object], expected: tuple[object, ...]
) -> None:
    result = _handle(backends, _call(tool, args))
    assert backends.calls == [expected]
    assert (result.id, result.call_id, result.ok, result.error) == ("req1", "call1", True, None)
    assert result.data == {"tool": expected[0]}


@pytest.mark.parametrize(
    ("tool", "args", "code"),
    [
        ("run_shell", {}, "unknown_tool"),
        ("inspect_file", {"handle": "x"}, "unknown_tool"),
        ("", {}, "unknown_tool"),
        ("get_app_state", {"extra": 1}, "bad_arguments"),
        ("get_system_stats", {}, "bad_arguments"),
        ("get_system_stats", {"topic": "journal"}, "bad_arguments"),
        ("man", {"name": "-P/bin/sh"}, "bad_arguments"),
        ("man", {"name": "ls", "section": 1}, "bad_arguments"),
        ("man", {"name": "ls", "section": "9"}, "bad_arguments"),
        ("man", {"name": "../../etc/passwd"}, "bad_arguments"),
        ("tldr", {"name": "tar; rm -rf ~"}, "bad_arguments"),
        ("tldr", {"name": "a" * 65}, "bad_arguments"),
    ],
)
def test_unknown_tools_and_bad_arguments_never_reach_a_backend(
    backends: Backends, tool: str, args: dict[str, object], code: str
) -> None:
    result = _handle(backends, _call(tool, args))
    assert (result.ok, result.error, result.data) == (False, code, None)
    assert backends.calls == []


def test_a_call_without_an_open_request_is_rejected(backends: Backends) -> None:
    result = _handle(backends, _call("tldr", {"name": "tar"}), request_open=False)
    assert (result.ok, result.error) == (False, "no_open_request")
    assert backends.calls == []


def test_the_per_request_cap(backends: Backends) -> None:
    last_allowed = MAX_TOOL_CALLS_PER_REQUEST - 1
    assert _handle(backends, _call("get_app_state"), calls_so_far=last_allowed).ok
    result = _handle(backends, _call("get_app_state"), calls_so_far=MAX_TOOL_CALLS_PER_REQUEST)
    assert (result.ok, result.error) == (False, "cap_reached")
    assert len(backends.calls) == 1


def test_a_backend_exception_becomes_a_code_not_its_text(backends: Backends) -> None:
    backends.fail = RuntimeError(f"/home/someone/{CANARY}")
    result = _handle(backends, _call("get_app_state"))
    assert (result.ok, result.error, result.data) == (False, "backend_failed", None)
    assert CANARY not in result.model_dump_json()


def test_a_backend_may_refuse_with_its_own_short_code(backends: Backends) -> None:
    backends.fail = ToolError("not_found")
    assert _handle(backends, _call("man", {"name": "nope"})).error == "not_found"
    backends.fail = ToolError(f"Not a code: {CANARY}")
    assert _handle(backends, _call("man", {"name": "nope"})).error == "backend_failed"


def test_a_slow_backend_times_out(backends: Backends) -> None:
    backends.hang = True
    result = _handle(backends, _call("get_app_state"), timeout=0.05)
    assert (result.ok, result.error) == (False, "timeout")


def test_the_default_timeout_is_ten_seconds(backends: Backends) -> None:
    assert HostToolDispatcher(backends)._timeout == 10.0


def test_result_size_cap(backends: Backends) -> None:
    overhead = len('{"t":""}')
    backends.reply = {"t": "x" * (MAX_TOOL_RESULT_BYTES - overhead)}
    assert _handle(backends, _call("get_app_state")).ok
    backends.reply = {"t": "x" * (MAX_TOOL_RESULT_BYTES - overhead + 1)}
    result = _handle(backends, _call("get_app_state"))
    assert (result.ok, result.error, result.data) == (False, "too_large", None)
    backends.reply = {"t": "é" * (MAX_TOOL_RESULT_BYTES // 2)}  # counted in bytes, not chars
    assert _handle(backends, _call("get_app_state")).error == "too_large"


@pytest.mark.parametrize(
    "reply",
    [
        ["a list"],
        "text",
        {"when": object()},
        {"t": "\ud800"},
        {1: "a key that is not text"},
        {"load": float("nan")},
        {"deep": {("a", "b"): 1}},
    ],
)
def test_a_result_that_is_not_a_json_object_is_refused(backends: Backends, reply: object) -> None:
    backends.reply = reply
    result = _handle(backends, _call("get_app_state"))
    assert (result.ok, result.error, result.data) == (False, "bad_result", None)


def test_rejections_are_logged_with_tool_enum_and_code_only(backends: Backends) -> None:
    # Own handler on Waddle's logger: independent of what other tests left on the root logger.
    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    logger = logging.getLogger("waddle")
    handler = _Collect(level=logging.DEBUG)
    before = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        backends.fail = RuntimeError(CANARY)
        _handle(backends, _call(CANARY[:20], {"name": CANARY}))
        _handle(backends, _call("man", {"name": CANARY + " "}))
        _handle(backends, _call("man", {"name": CANARY}))
        _handle(backends, _call("tldr", {"name": CANARY}), request_open=False)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(before)
    lines = [line for line in lines if "tool_rejected" in line]
    assert len(lines) == 4
    assert '"tool":"unknown","code":"unknown_tool"' in lines[0]
    assert '"tool":"man","code":"bad_arguments"' in lines[1]
    assert '"tool":"man","code":"backend_failed"' in lines[2]
    assert '"tool":"tldr","code":"no_open_request"' in lines[3]
    assert CANARY not in "\n".join(lines)
