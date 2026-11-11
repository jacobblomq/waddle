"""Structured, redacted logging (ARCHITECTURE §5.6).

Log lines are an allowlist: an event name plus numbers, booleans and short identifier-like
strings. Anything else (window titles, paths, text) is replaced by a placeholder, so screen
content cannot reach the logs by accident.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Final

_SAFE: Final = re.compile(r"^[A-Za-z0-9_.:+-]{1,64}$")
REDACTED: Final = "<redacted>"

_logger = logging.getLogger("waddle")
_unredacted = False


def configure(level: str = "info", *, debug_unredacted: bool = False) -> None:
    global _unredacted
    _unredacted = debug_unredacted
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.handlers[:] = [handler]
    _logger.setLevel(level.upper())
    _logger.propagate = False


def _clean(value: object) -> object:
    if isinstance(value, bool | int | float) or value is None:
        return value
    text = str(value)
    if _unredacted or _SAFE.match(text):
        return text
    return REDACTED


def render(name: str, fields: dict[str, object]) -> str:
    record: dict[str, object] = {"t": round(time.time(), 3), "event": _clean(name)}
    record.update({key: _clean(value) for key, value in fields.items()})
    return json.dumps(record, separators=(",", ":"))


def event(name: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Log one event. Pass only ids, enums, counts, sizes and timings as fields."""
    if _logger.isEnabledFor(level):
        _logger.log(level, render(name, fields))


def warning(name: str, **fields: object) -> None:
    event(name, level=logging.WARNING, **fields)
