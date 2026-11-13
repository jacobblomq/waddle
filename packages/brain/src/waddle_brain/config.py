"""Brain configuration: where the socket and token are, and which model plays which role.

Model names live only in the config file (D-018). The shipped defaults are in
``deploy/brain.toml``; they are provisional until the bake-off.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from waddle_core.link import Role


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
    num_ctx: int = Field(8192, ge=1024, le=262_144)  # fixed: changing it reloads the model
    keep_alive_s: int = Field(300, ge=0, le=86_400)  # 0 = unload right after use
    vision: bool = False
    # Thinking models reason before the first word of the reply. None leaves the model
    # server's default alone; true or false is sent as the request's ``think`` setting.
    think: bool | None = None


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_answer_chars: int = Field(6000, ge=200, le=50_000)
    answer_timeout_s: float = Field(120.0, gt=0, le=600)
    first_token_timeout_s: float = Field(60.0, gt=0, le=600)
    load_timeout_s: float = Field(180.0, gt=0, le=1800)


class BrainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["ollama"] = "ollama"
    ollama_url: str = Field("http://ollama:11434", pattern=r"^http://[A-Za-z0-9.-]+(:\d{1,5})?$")
    socket_path: Path = Path("/run/waddle/brain.sock")
    token_path: Path = Path("/run/secrets/waddle-link-token")
    roles: dict[Role, RoleConfig]
    limits: LimitsConfig = Field(default_factory=LimitsConfig)


class BrainConfigError(ValueError):
    """The brain's config file is missing or not valid."""


def load_brain_config(path: Path) -> BrainConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return BrainConfig.model_validate(data)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise BrainConfigError(type(exc).__name__) from None
