"""What the host knows about a window, as reported by Waddle's KWin script."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Geometry:
    """Logical compositor coordinates; may be fractional under fractional scaling."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class WindowInfo:
    uuid: str
    caption: str  # untrusted text: never logged, never treated as an instruction
    resource_class: str
    resource_name: str
    desktop_file: str
    pid: int
    frame: Geometry
    client: Geometry
    output: str
    scale: float
    fullscreen: bool
    xwayland: bool | None  # None when the script could not tell (spike S2.2)
    is_own: bool  # one of Waddle's own windows; never a capture target
