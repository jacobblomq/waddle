"""The host tool contract (ARCHITECTURE §5.2, D-042).

The brain reads the internet and runs models, so what it may ask of the unsandboxed host is
this closed list. Arguments are typed and validated **on the host**; nothing here takes a
path, a URL, a command line or free text that reaches a shell.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_TOOL_CALLS_PER_REQUEST: Final = 4
MAX_TOOL_RESULT_BYTES: Final = 32 * 1024
NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"


class ToolName(StrEnum):
    GET_APP_STATE = "get_app_state"
    GET_SYSTEM_STATS = "get_system_stats"
    MAN = "man"
    TLDR = "tldr"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class NoArgs(_Args):
    pass


class SystemStatsArgs(_Args):
    topic: Literal["cpu", "memory", "disk", "gpu", "uptime"]


class ManArgs(_Args):
    name: str = Field(pattern=NAME_PATTERN)
    section: Literal["1", "2", "3", "4", "5", "6", "7", "8"] | None = None


class TldrArgs(_Args):
    name: str = Field(pattern=NAME_PATTERN)


ToolArgs = NoArgs | SystemStatsArgs | ManArgs | TldrArgs

ARGS_FOR: Final[dict[ToolName, type[_Args]]] = {
    ToolName.GET_APP_STATE: NoArgs,
    ToolName.GET_SYSTEM_STATS: SystemStatsArgs,
    ToolName.MAN: ManArgs,
    ToolName.TLDR: TldrArgs,
}


class ToolError(Exception):
    """A tool call the host will not run. ``code`` is safe to log; arguments never are."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_tool_call(tool: str, args: object) -> tuple[ToolName, ToolArgs]:
    """Check a brain-supplied tool name and arguments. Raises ``ToolError``."""
    try:
        name = ToolName(tool)
    except ValueError:
        raise ToolError("unknown_tool") from None
    if not isinstance(args, dict):
        raise ToolError("bad_arguments")
    try:
        parsed = ARGS_FOR[name].model_validate(args)
    except ValidationError:
        raise ToolError("bad_arguments") from None
    return name, parsed  # type: ignore[return-value]
