"""Typed payloads sent by Waddle's KWin scripts, and their parsers.

The payloads arrive as JSON strings over the session bus, where any process can call us, so
they are untrusted input: every parser validates types, ranges and lengths and raises
``ValueError`` on anything else. Error messages never contain payload content.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Final

MAX_PAYLOAD_CHARS: Final = 16_384
MAX_STRING_CHARS: Final = 256
MAX_OUTPUTS: Final = 16
MAX_COORD: Final = 100_000  # logical pixels, either sign
MAX_SIZE: Final = 100_000


@dataclass(frozen=True, slots=True)
class OutputInfo:
    name: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class OwnWindow:
    title: str
    uuid: str
    x: int
    y: int
    width: int
    height: int
    output: str


@dataclass(frozen=True, slots=True)
class Hello:
    version: int
    api_ok: bool


@dataclass(frozen=True, slots=True)
class MoveDone:
    title: str
    ok: bool
    x: int
    y: int


def _reject_constant(_name: str) -> float:
    raise ValueError("non-finite number")


def _object(payload: object) -> dict[str, object]:
    if not isinstance(payload, str):
        raise ValueError("payload is not a string")
    if len(payload) > MAX_PAYLOAD_CHARS:
        raise ValueError("payload too long")
    try:
        data = json.loads(payload, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("payload is not JSON") from exc
    return _dict(data)


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def _str(data: dict[str, object], key: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key}: expected a string")
    if len(value) > MAX_STRING_CHARS or (not value and not allow_empty):
        raise ValueError(f"{key}: bad length")
    if not value.isprintable():  # control characters, lone surrogates
        raise ValueError(f"{key}: unprintable characters")
    return value


def _bool(data: dict[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key}: expected a boolean")
    return value


def _int(data: dict[str, object], key: str, low: int, high: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key}: expected a number")
    # An int is range-checked as it is: a huge one would overflow a float conversion.
    if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
        raise ValueError(f"{key}: expected a whole number")
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{key}: out of range")
    return number


def _coord(data: dict[str, object], key: str) -> int:
    return _int(data, key, -MAX_COORD, MAX_COORD)


def _size(data: dict[str, object], key: str) -> int:
    return _int(data, key, 1, MAX_SIZE)


def parse_hello(payload: str) -> Hello:
    data = _object(payload)
    return Hello(version=_int(data, "version", 0, 1_000_000), api_ok=_bool(data, "apiOk"))


def parse_own_window(payload: str) -> OwnWindow:
    data = _object(payload)
    return OwnWindow(
        title=_str(data, "title"),
        uuid=_str(data, "uuid"),
        x=_coord(data, "x"),
        y=_coord(data, "y"),
        width=_size(data, "width"),
        height=_size(data, "height"),
        output=_str(data, "output", allow_empty=True),
    )


def parse_outputs(payload: str) -> tuple[list[OutputInfo], str | None]:
    """Parse ``{"outputs": [{name, x, y, width, height}, ...], "active": name | null}``."""
    data = _object(payload)
    entries = data.get("outputs")
    if not isinstance(entries, list) or len(entries) > MAX_OUTPUTS:
        raise ValueError("outputs: expected a short list")
    outputs: list[OutputInfo] = []
    for entry in entries:
        item = _dict(entry)
        outputs.append(
            OutputInfo(
                name=_str(item, "name"),
                x=_coord(item, "x"),
                y=_coord(item, "y"),
                width=_size(item, "width"),
                height=_size(item, "height"),
            )
        )
    active = None if data.get("active") is None else _str(data, "active")
    return outputs, active


def parse_move_done(payload: str) -> MoveDone:
    data = _object(payload)
    return MoveDone(
        title=_str(data, "title"),
        ok=_bool(data, "ok"),
        x=_coord(data, "x"),
        y=_coord(data, "y"),
    )
