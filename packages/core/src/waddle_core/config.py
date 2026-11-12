"""Configuration schema for ~/.config/waddle/config.toml. Every key has a documented default."""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UiConfig(_Section):
    style: Literal["minimal", "detailed"] = Field(
        "minimal", description="'minimal' is a small flat icon; 'detailed' is the larger character."
    )
    size_scale: float = Field(1.0, ge=0.5, le=2.5, description="Penguin size multiplier.")
    reduced_motion: Literal["auto", "on", "off"] = Field(
        "auto", description="'auto' follows Plasma's animation speed setting."
    )
    chattiness: float = Field(0.5, ge=0.0, le=1.0, description="0 = quiet, 1 = chatty.")
    bubble_max_lines: int = Field(4, ge=1, le=20, description="Lines shown before 'more'.")


class PlacementConfig(_Section):
    dodge: bool = Field(True, description="Step aside when the pointer rests on the penguin.")
    dodge_dwell_ms: int = Field(350, ge=0, le=5000)
    dodge_return_ms: int = Field(4000, ge=500, le=60000)
    margin_px: int = Field(24, ge=0, le=400, description="Gap to the screen edge.")


class PrivacyConfig(_Section):
    block_classes: tuple[str, ...] = Field(
        (), description="Extra window classes Waddle must never look at (substring match)."
    )
    block_titles: tuple[str, ...] = Field(
        (), description="Extra window-title fragments Waddle must never look at."
    )


class WatchingConfig(_Section):
    glance: bool = Field(True, description="One low-res look on app switch when needed.")
    ambient: bool = Field(False, description="Occasional looks for proactive tips. Opt-in.")
    guide_max_fps: float = Field(1.0, gt=0, le=1.0, description="Cap while a guide is active.")
    fullscreen: Literal["badge", "hide", "stay"] = Field(
        "badge", description="What Waddle does when the focused app is fullscreen."
    )
    hide_during_screencast: bool = Field(False, description="Hide while another app records.")


class HotkeysConfig(_Section):
    """Preferred keys; the installer checks each against your existing shortcuts first."""

    ask: str = "Meta+Space"
    push_to_talk: str = "Meta+`"
    pause: str = "Meta+Shift+W"
    show_me_where: str = "Meta+Shift+H"
    region_select: str = "Meta+Shift+R"
    quiet: str = "Meta+Shift+Q"
    focus_mode: str = "Meta+Shift+F"
    bring_back: str = "Meta+Shift+B"


class LoggingConfig(_Section):
    level: Literal["debug", "info", "warning", "error"] = "info"
    debug_unredacted: bool = Field(
        False, description="DANGER: allows screen text in logs. Off unless you are debugging."
    )


class WaddleConfig(_Section):
    ui: UiConfig = Field(default_factory=UiConfig)
    placement: PlacementConfig = Field(default_factory=PlacementConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    watching: WatchingConfig = Field(default_factory=WatchingConfig)
    hotkeys: HotkeysConfig = Field(default_factory=HotkeysConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class ConfigError(ValueError):
    """The config file exists but is not valid."""


def load_config(path: Path) -> WaddleConfig:
    """Load config from ``path``; a missing file yields the defaults."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return WaddleConfig()
    try:
        return WaddleConfig.model_validate(tomllib.loads(raw.decode("utf-8")))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def default_config_toml() -> str:
    """A fully commented config file showing every key at its default."""
    lines = ["# Waddle configuration. Delete a line to return to its default.", ""]
    for name, field in WaddleConfig.model_fields.items():
        section = field.annotation
        assert isinstance(section, type) and issubclass(section, BaseModel)
        lines.append(f"[{name}]")
        for key, sub in section.model_fields.items():
            if sub.description:
                lines.append(f"# {sub.description}")
            default = sub.default
            if isinstance(default, bool):
                rendered = "true" if default else "false"
            elif isinstance(default, tuple):
                rendered = "[" + ", ".join(f'"{v}"' for v in default) + "]"
            elif isinstance(default, StrEnum | str):
                rendered = f'"{default!s}"'
            else:
                rendered = repr(default)
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines)
