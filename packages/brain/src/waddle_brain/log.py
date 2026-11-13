"""Structured, redacted logging for the brain (ARCHITECTURE §5.6).

Log lines are an allowlist: an event name plus numbers, booleans and short identifier-like
strings. Anything else is replaced by a placeholder. The filter is a safety net, not the
rule: callers pass only event names, enum values, counts, sizes and timings, and never a
string that came from the host, a tool or a model.
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

_logger = logging.getLogger("waddle_brain")


def configure(level: str = "info") -> None:
    """Send events to stderr, one JSON object per line."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.handlers[:] = [handler]
    _logger.setLevel(level.upper())
    _logger.propagate = False


def _clean(value: object) -> object:
    if isinstance(value, bool | int | float) or value is None:
        return value
    text = str(value)
    return text if _SAFE.match(text) else REDACTED


def render(name: str, fields: dict[str, object]) -> str:
    record: dict[str, object] = {"t": round(time.time(), 3), "event": _clean(name)}
    record.update({key: _clean(value) for key, value in fields.items()})
    return json.dumps(record, separators=(",", ":"))


def event(name: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Log one event. Pass only enums, counts, sizes and timings as fields."""
    if _logger.isEnabledFor(level):
        _logger.log(level, render(name, fields))


def warning(name: str, **fields: object) -> None:
    event(name, level=logging.WARNING, **fields)
