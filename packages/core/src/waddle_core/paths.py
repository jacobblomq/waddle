"""XDG locations for Waddle's own files (ARCHITECTURE §7). Pure: the environment is passed in."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DIR = "waddle"


@dataclass(frozen=True)
class WaddlePaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    runtime_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def desktop_db(self) -> Path:
        return self.data_dir / "desktop.db"

    @property
    def install_manifest(self) -> Path:
        return self.data_dir / "install-manifest.json"

    @property
    def pid_file(self) -> Path:
        return self.runtime_dir / "desktop.pid"

    def all_dirs(self) -> tuple[Path, ...]:
        return (self.config_dir, self.data_dir, self.cache_dir, self.runtime_dir)


def resolve_paths(env: Mapping[str, str], home: Path) -> WaddlePaths:
    def xdg(var: str, default: Path) -> Path:
        value = env.get(var, "")
        return Path(value) if value.startswith("/") else default

    runtime = xdg("XDG_RUNTIME_DIR", xdg("XDG_CACHE_HOME", home / ".cache") / "run")
    return WaddlePaths(
        config_dir=xdg("XDG_CONFIG_HOME", home / ".config") / _DIR,
        data_dir=xdg("XDG_DATA_HOME", home / ".local" / "share") / _DIR,
        cache_dir=xdg("XDG_CACHE_HOME", home / ".cache") / _DIR,
        runtime_dir=runtime / _DIR,
    )
