import os
from pathlib import Path

import pytest

from waddle_core.paths import resolve_paths
from waddle_desktop.cli import main as cli


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    return tmp_path


def test_status_and_stop_when_not_running(xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 1
    assert cli.main(["stop"]) == 0
    assert capsys.readouterr().out.count("not running") == 2


def test_stale_or_foreign_pid_file_is_never_signalled(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = resolve_paths(os.environ, Path.home())
    paths.runtime_dir.mkdir(parents=True)
    killed: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))

    for content in ("1", str(os.getpid()), "not a pid", ""):  # nobody holds the lock
        paths.pid_file.write_text(content)
        assert cli.running_pid(paths) is None
        assert cli.main(["stop"]) == 0
    assert killed == []


def test_status_reports_memory_of_a_running_process(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = resolve_paths(os.environ, Path.home())
    paths.runtime_dir.mkdir(parents=True)
    paths.pid_file.write_text(str(os.getpid()))
    monkeypatch.setattr(cli, "running_pid", lambda _paths: os.getpid())
    assert cli.main(["status", "--measure", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "memory (PSS)" in out and "CPU:" in out


def test_bad_config_stops_start_with_a_clear_error(
    xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = resolve_paths(os.environ, Path.home())
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text("[ui]\nsize_scale = 99\n")
    assert cli.main(["start"]) == 2
    assert "Config error" in capsys.readouterr().err


def test_config_print_default_is_valid_toml(capsys: pytest.CaptureFixture[str]) -> None:
    import tomllib

    assert cli.main(["config", "--print-default"]) == 0
    assert "ui" in tomllib.loads(capsys.readouterr().out)
