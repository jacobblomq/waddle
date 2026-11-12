"""Where Waddle's own read-only data files live (KWin scripts, the default blocklist)."""

from __future__ import annotations

from pathlib import Path

import waddle_desktop

_PACKAGE_DIR = Path(waddle_desktop.__file__).parent


def data_base() -> Path:
    """The directory holding ``kwin/`` and ``knowledge/``: packaged data, or the repo root."""
    packaged = _PACKAGE_DIR / "_data"
    return packaged if (packaged / "kwin").is_dir() else _PACKAGE_DIR.parents[3]


def default_blocklist_path(base: Path | None = None) -> Path:
    return (base or data_base()) / "knowledge" / "privacy" / "default-blocklist.json"
