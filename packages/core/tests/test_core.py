import tomllib
from pathlib import Path

import pytest

from waddle_core.config import ConfigError, WaddleConfig, default_config_toml, load_config
from waddle_core.guide import mock_guide
from waddle_core.mood import STEADY_MOODS, Mood
from waddle_core.paths import resolve_paths


def test_all_eleven_moods_exist() -> None:
    assert len(Mood) == 11
    assert {m.value for m in Mood} >= {"idle", "eyes-closed", "napping-brain", "sleeping"}
    assert Mood.CELEBRATE not in STEADY_MOODS and Mood.IDLE in STEADY_MOODS


def test_default_config_round_trips() -> None:
    assert WaddleConfig.model_validate(tomllib.loads(default_config_toml())) == WaddleConfig()


def test_missing_config_is_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "nope.toml") == WaddleConfig()


@pytest.mark.parametrize("body", ["[ui]\nsize_scale = 9", "[ui]\nunknown = 1", "not toml ["])
def test_bad_config_is_rejected(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body)
    with pytest.raises(ConfigError):
        load_config(path)


def test_paths_follow_xdg_and_ignore_relative_values() -> None:
    home = Path("/home/u")
    paths = resolve_paths({"XDG_CONFIG_HOME": "/c", "XDG_DATA_HOME": "relative"}, home)
    assert paths.config_file == Path("/c/waddle/config.toml")
    assert paths.data_dir == home / ".local/share/waddle"
    assert all(p.name == "waddle" for p in paths.all_dirs())


def test_mock_guide_view() -> None:
    view = mock_guide().to_qml()
    assert view["progressLabel"] == "Step 2 of 5"
    steps = view["steps"]
    assert isinstance(steps, list) and steps[0]["segments"][1]["keys"] == ["Shift", "A"]
