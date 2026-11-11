"""Read Plasma's colors, font and animation speed from ``kdeglobals`` (D-006). Pure; no Qt.

Waddle only ever reads these files.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

from waddle_desktop.theme.palette import FALLBACK_DARK, FALLBACK_LIGHT, ThemePalette

ReducedMotion = Literal["auto", "on", "off"]

_MAX_BYTES = 1 << 20  # a real kdeglobals is a few kB; never slurp something huge
_GROUP_FLAGS = re.compile(r"(\[\$[^\]]*\])+$")
_KEY_SUFFIX = re.compile(r"\[([^\]]*)\]")
_HEX = re.compile(r"#[0-9a-fA-F]{6}")

# palette field -> (group, key)
_COLOR_KEYS: dict[str, tuple[str, str]] = {
    "window_bg": ("Colors:Window", "BackgroundNormal"),
    "window_fg": ("Colors:Window", "ForegroundNormal"),
    "view_bg": ("Colors:View", "BackgroundNormal"),
    "view_fg": ("Colors:View", "ForegroundNormal"),
    "link": ("Colors:View", "ForegroundLink"),
    "positive": ("Colors:View", "ForegroundPositive"),
    "negative": ("Colors:View", "ForegroundNegative"),
    "neutral": ("Colors:View", "ForegroundNeutral"),
    "button_bg": ("Colors:Button", "BackgroundNormal"),
    "button_fg": ("Colors:Button", "ForegroundNormal"),
    "highlight": ("Colors:Selection", "BackgroundNormal"),
    "highlighted_fg": ("Colors:Selection", "ForegroundNormal"),
}


def parse_kconfig(text: str) -> dict[str, dict[str, str]]:
    """Parse one KConfig INI file into ``{group: {key: value}}``.

    Nested groups keep their inner brackets (``[A][B]`` -> ``"A][B"``). ``[$i]``-style flags are
    stripped from keys and group headers; localized keys (``Name[de]``) are ignored. Entries
    before the first header land in group ``""``.
    """
    groups: dict[str, dict[str, str]] = {}
    group: str | None = ""
    for raw in text.removeprefix("\ufeff").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            header = _GROUP_FLAGS.sub("", line)
            group = header[1:-1] if header.endswith("]") and len(header) > 2 else None
            if group is not None:
                groups.setdefault(group, {})
            continue
        if group is None or "=" not in line:
            continue  # inside a malformed header, or not an entry
        key, value = line.split("=", 1)
        name, bracket, suffixes = key.strip().partition("[")
        if any(not s.startswith("$") for s in _KEY_SUFFIX.findall(bracket + suffixes)):
            continue  # localized variant
        name = name.strip()
        if name:
            groups.setdefault(group, {})[name] = value.strip()
    return groups


def _parse_color(value: str | None) -> str | None:
    """``r,g,b`` / ``r,g,b,a`` / ``#rrggbb`` -> ``#rrggbb``; anything else -> None."""
    if value is None:
        return None
    value = value.strip()
    if _HEX.fullmatch(value):
        return value.lower()
    parts = [p.strip() for p in value.split(",")]
    if len(parts) not in (3, 4) or not all(p.isascii() and p.isdigit() for p in parts):
        return None
    channels = [int(p) for p in parts]
    if any(c > 255 for c in channels):
        return None
    return "#{:02x}{:02x}{:02x}".format(*channels[:3])


def _luminance(color: str) -> float:
    """WCAG relative luminance of an sRGB ``#rrggbb`` color, 0 (black) to 1 (white)."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else float(((c + 0.055) / 1.055) ** 2.4)

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def _parse_font(value: str | None, fallback: ThemePalette) -> tuple[str, float]:
    """Qt font string ``Family,pointSize,...``; each field falls back on its own."""
    fields = (value or "").split(",")
    family = fields[0].strip() or fallback.font_family
    size = fallback.font_point_size
    if len(fields) > 1:
        try:
            parsed = float(fields[1])
        except ValueError:
            parsed = -1.0
        if math.isfinite(parsed) and parsed > 0:
            size = parsed
    return family, size


def _animation_factor(value: str | None, reduced_motion: ReducedMotion) -> float:
    try:
        factor = float(value) if value is not None else 1.0
    except ValueError:
        factor = 1.0
    factor = min(max(factor, 0.0), 10.0) if math.isfinite(factor) else 1.0
    if reduced_motion == "on":
        return 0.0
    if reduced_motion == "off" and factor <= 0:
        return 1.0
    return factor


def palette_from_kdeglobals(
    layers: Sequence[str], *, reduced_motion: ReducedMotion = "auto"
) -> ThemePalette:
    """Build a palette from file contents given in increasing priority (user's file last)."""
    merged: dict[str, dict[str, str]] = {}
    for text in layers:
        for group, entries in parse_kconfig(text).items():
            merged.setdefault(group, {}).update(entries)

    def get(group: str, key: str) -> str | None:
        return merged.get(group, {}).get(key)

    parsed = {field: _parse_color(get(*where)) for field, where in _COLOR_KEYS.items()}
    window_bg = parsed["window_bg"]
    is_dark = window_bg is not None and _luminance(window_bg) < 0.5
    base = FALLBACK_DARK if is_dark else FALLBACK_LIGHT

    colors = {field: color or getattr(base, field) for field, color in parsed.items()}
    family, size = _parse_font(get("General", "font"), base)
    return replace(
        base,
        **colors,
        accent=_parse_color(get("General", "AccentColor")) or colors["highlight"],
        is_dark=is_dark,
        font_family=family,
        font_point_size=size,
        animation_factor=_animation_factor(get("KDE", "AnimationDurationFactor"), reduced_motion),
    )


def kdeglobals_paths(env: Mapping[str, str], home: Path) -> list[Path]:
    """Candidate files in increasing priority: system dirs (last-listed first), then the user's."""

    def absolute(value: str) -> list[Path]:
        return [Path(p) for p in value.split(":") if p.startswith("/")]

    system = absolute(env.get("XDG_CONFIG_DIRS", "")) or [Path("/etc/xdg")]
    config_home = env.get("XDG_CONFIG_HOME", "")  # one directory, not a list: ":" is literal
    user = Path(config_home) if config_home.startswith("/") else home / ".config"
    return [d / "kdeglobals" for d in [*reversed(system), user]]


def load_palette(
    env: Mapping[str, str], home: Path, reduced_motion: ReducedMotion = "auto"
) -> ThemePalette:
    """Read whatever kdeglobals files exist. Never raises; nothing readable -> fallback."""
    layers: list[str] = []
    for path in kdeglobals_paths(env, home):
        try:
            with path.open("rb") as handle:
                layers.append(handle.read(_MAX_BYTES).decode("utf-8", errors="replace"))
        except OSError:
            continue
    return palette_from_kdeglobals(layers, reduced_motion=reduced_motion)
