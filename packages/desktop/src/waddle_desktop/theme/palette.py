"""The colors and fonts Waddle takes from Plasma (D-006). Plain data; no Qt."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ThemePalette:
    """All colors are ``#rrggbb`` strings."""

    accent: str
    window_bg: str
    window_fg: str
    view_bg: str
    view_fg: str
    button_bg: str
    button_fg: str
    highlight: str
    highlighted_fg: str
    positive: str
    negative: str
    neutral: str
    link: str
    is_dark: bool
    font_family: str
    font_point_size: float
    animation_factor: float  # Plasma's AnimationDurationFactor: 1 = normal, 0 = animations off

    def with_animation_factor(self, factor: float) -> ThemePalette:
        return replace(self, animation_factor=factor)


FALLBACK_LIGHT = ThemePalette(
    accent="#3daee9",
    window_bg="#eff0f1",
    window_fg="#232629",
    view_bg="#ffffff",
    view_fg="#232629",
    button_bg="#fcfcfc",
    button_fg="#232629",
    highlight="#3daee9",
    highlighted_fg="#ffffff",
    positive="#27ae60",
    negative="#da4453",
    neutral="#f67400",
    link="#2980b9",
    is_dark=False,
    font_family="Noto Sans",
    font_point_size=10.0,
    animation_factor=1.0,
)

FALLBACK_DARK = replace(
    FALLBACK_LIGHT,
    window_bg="#202326",
    window_fg="#fcfcfc",
    view_bg="#141618",
    view_fg="#fcfcfc",
    button_bg="#292c30",
    button_fg="#fcfcfc",
    highlighted_fg="#fcfcfc",
    link="#1d99f3",
    is_dark=True,
)
