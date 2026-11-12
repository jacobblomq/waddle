"""Typed payloads sent by Waddle's KWin scripts, and their parsers.

The payloads arrive as JSON strings over the session bus, where any process can call us, so
they are untrusted input: every parser validates types, ranges and lengths and raises
``ValueError`` on anything else. Error messages never contain payload content.

Text that another application chose (window titles, classes, output names in a window
report) is arbitrary, so it is cleaned instead of rejected: characters that do not print are
dropped and the rest is cut to a fixed length. It stays untrusted after that.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Final

from waddle_core.window import Geometry, WindowInfo

MAX_PAYLOAD_CHARS: Final = 16_384
MAX_STRING_CHARS: Final = 256
MAX_OUTPUTS: Final = 16
MAX_COORD: Final = 100_000  # logical pixels, either sign
MAX_SIZE: Final = 100_000
MAX_CAPTION_CHARS: Final = 512
MAX_POPUPS: Final = 16
MAX_SCALE: Final = 16.0
MAX_PID: Final = 2**31 - 1

_UUID: Final = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


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


@dataclass(frozen=True, slots=True)
class Popup:
    """A transient popup (menu, combo list) of the target, in logical coordinates."""

    uuid: str
    geometry: Geometry


@dataclass(frozen=True, slots=True)
class PopupSet:
    """Every popup the target has open right now; an empty tuple when the last one closed."""

    target: str  # uuid of the window the popups belong to
    popups: tuple[Popup, ...]


@dataclass(frozen=True, slots=True)
class CursorPos:
    """The pointer in logical compositor coordinates."""

    x: float
    y: float


@dataclass(frozen=True, slots=True)
class WindowAt:
    """Answer to "which window is under logical (x, y)": a window, or None for none."""

    x: int
    y: int
    window: WindowInfo | None


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


def _text(data: dict[str, object], key: str, limit: int = MAX_STRING_CHARS) -> str:
    """Text chosen by another application: cleaned and cut, never rejected for its content."""
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key}: expected a string")
    # isprintable() is False for control and format characters, lone surrogates and line
    # separators: the things that could break a log line, a prompt block or a label.
    return "".join(ch for ch in value if ch.isprintable())[:limit]


def _number(data: dict[str, object], key: str, low: float, high: float) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key}: expected a number")
    # Compared before any conversion: a huge int would overflow float(), and every
    # comparison with NaN is False.
    if not low <= value <= high:
        raise ValueError(f"{key}: out of range")
    return float(value)


def _uuid(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise ValueError(f"{key}: expected a uuid")
    return value


def _geometry(data: dict[str, object]) -> Geometry:
    return Geometry(
        x=_number(data, "x", -MAX_COORD, MAX_COORD),
        y=_number(data, "y", -MAX_COORD, MAX_COORD),
        width=_number(data, "width", 0, MAX_SIZE),
        height=_number(data, "height", 0, MAX_SIZE),
    )


def _window_info(data: dict[str, object]) -> WindowInfo:
    xwayland = data.get("xwayland")
    if xwayland is not None and not isinstance(xwayland, bool):
        raise ValueError("xwayland: expected a boolean or null")
    scale = _number(data, "scale", 0, MAX_SCALE)
    if scale <= 0:
        raise ValueError("scale: out of range")
    return WindowInfo(
        uuid=_uuid(data, "uuid"),
        caption=_text(data, "caption", MAX_CAPTION_CHARS),
        resource_class=_text(data, "resourceClass"),
        resource_name=_text(data, "resourceName"),
        desktop_file=_text(data, "desktopFile"),
        pid=_int(data, "pid", 0, MAX_PID),
        frame=_geometry(_dict(data.get("frame"))),
        client=_geometry(_dict(data.get("client"))),
        output=_text(data, "output"),
        scale=scale,
        fullscreen=_bool(data, "fullscreen"),
        xwayland=xwayland,
        is_own=_bool(data, "own"),
    )


def parse_window_info(payload: str) -> WindowInfo:
    """Parse a window report: the payload of ``Activated`` and of ``TargetChanged``."""
    return _window_info(_object(payload))


parse_activated = parse_window_info
parse_target_changed = parse_window_info


def parse_popups(payload: str) -> PopupSet:
    """Parse ``{"target": uuid, "popups": [{uuid, x, y, width, height}, ...]}``."""
    data = _object(payload)
    entries = data.get("popups")
    if not isinstance(entries, list) or len(entries) > MAX_POPUPS:
        raise ValueError("popups: expected a short list")
    popups: list[Popup] = []
    for entry in entries:
        item = _dict(entry)
        popups.append(Popup(uuid=_uuid(item, "uuid"), geometry=_geometry(item)))
    return PopupSet(target=_uuid(data, "target"), popups=tuple(popups))


def parse_cursor(payload: str) -> CursorPos:
    data = _object(payload)
    return CursorPos(
        x=_number(data, "x", -MAX_COORD, MAX_COORD),
        y=_number(data, "y", -MAX_COORD, MAX_COORD),
    )


def parse_desktop_changed(payload: str) -> None:
    """``DesktopChanged`` carries an object with nothing in it that the host reads."""
    _object(payload)


def parse_window_at(payload: str) -> WindowAt:
    """Parse ``{"x": int, "y": int, "window": <window report> | null}``."""
    data = _object(payload)
    if "window" not in data:
        raise ValueError("window: missing")
    window = data["window"]
    return WindowAt(
        x=_coord(data, "x"),
        y=_coord(data, "y"),
        window=None if window is None else _window_info(_dict(window)),
    )


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
