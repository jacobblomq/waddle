"""The wheel carries the app packs where ``default_packs_dir()`` looks for them."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from waddle_core.privacy import load_blocklist
from waddle_desktop import resources
from waddle_desktop.identity import packs

DESKTOP = Path(__file__).resolve().parents[1]
REPO = DESKTOP.parents[1]


def _force_include() -> dict[str, str]:
    data = tomllib.loads((DESKTOP / "pyproject.toml").read_text(encoding="utf-8"))
    table: dict[str, str] = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    return table


def test_app_packs_are_force_included_from_the_repo_folder() -> None:
    sources = {(DESKTOP / source).resolve(): target for source, target in _force_include().items()}
    assert sources.get(REPO / "knowledge" / "apps") == "waddle_desktop/_data/knowledge/apps"
    assert any((REPO / "knowledge" / "apps").glob("*.toml"))


def test_default_packs_dir_is_the_packaged_place_in_a_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lay the force-included files out as a wheel would, and ask where packs are read."""
    package = tmp_path / "waddle_desktop"
    for source, target in _force_include().items():
        placed = tmp_path / target
        placed.parent.mkdir(parents=True, exist_ok=True)
        origin = (DESKTOP / source).resolve()
        if origin.is_dir():
            placed.symlink_to(origin, target_is_directory=True)
        else:
            placed.write_bytes(origin.read_bytes())
    monkeypatch.setattr(resources, "_PACKAGE_DIR", package)
    found = packs.default_packs_dir()
    assert found == package / "_data" / "knowledge" / "apps"
    index = packs.load_packs(found)
    assert index.packs and not index.errors


def test_a_wheel_has_every_data_file_waddle_reads_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both KWin scripts and the default private-window list; without them Waddle will not start."""
    package = tmp_path / "waddle_desktop"
    for source, target in _force_include().items():
        placed = tmp_path / target
        placed.parent.mkdir(parents=True, exist_ok=True)
        placed.symlink_to((DESKTOP / source).resolve())
    monkeypatch.setattr(resources, "_PACKAGE_DIR", package)
    base = resources.data_base()
    assert base == package / "_data"
    for script in ("waddle-core.js", "waddle-cursor.js"):
        assert (base / "kwin" / script).is_file(), script
    assert load_blocklist(resources.default_blocklist_path())
