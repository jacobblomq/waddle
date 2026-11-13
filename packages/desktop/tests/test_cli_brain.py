"""``waddle brain`` and ``waddle models``: every action goes through (a stand-in) BrainCtl."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fake_brain as fb

from waddle_core.link import ModelStatus, Role
from waddle_core.paths import WaddlePaths, resolve_paths
from waddle_desktop.brainctl import compose
from waddle_desktop.brainctl.compose import (
    BrainCtl,
    ComposeError,
    Logs,
    Outcome,
    Result,
    ServiceStatus,
    Status,
)
from waddle_desktop.cli import brain_cmd, models_cmd
from waddle_desktop.cli import main as cli
from waddle_desktop.cli.models_cmd import Probe

REPO = Path(__file__).resolve().parents[3]
OK = Result(Outcome.OK)
FAILED = Result(Outcome.FAILED, "Docker reported an error.")
UNAVAILABLE = Result(Outcome.UNAVAILABLE, "Docker is not installed.")
NAMES: dict[Role, str] = {"fast_vlm": "fast-x:1", "smart_model": "smart-x:2", "embedder": "emb-x:3"}
TOKEN = "ab" * 32


class FakeCtl:
    """Records calls; answers as told. ``running`` scripts ``native_running()``."""

    def __init__(self, paths: WaddlePaths | None = None) -> None:
        self.paths = paths
        self.calls: list[tuple[object, ...]] = []
        self.up_result = OK
        self.down_result = OK
        self.status_result = Status(Outcome.OK)
        self.logs_result = Logs(Outcome.OK, "", "line one\nline two\n")
        self.pull_results: dict[str, Result] = {}
        self.native_result = OK
        self.running: list[bool] = []
        self.token_seen: bool | None = None

    def up(self) -> Result:
        self.calls.append(("up",))
        if self.paths is not None:
            self.token_seen = self.paths.link_token.is_file()
        return self.up_result

    def down(self) -> Result:
        self.calls.append(("down",))
        return self.down_result

    def status(self) -> Status:
        self.calls.append(("status",))
        return self.status_result

    def logs(self, tail: int = 200) -> Logs:
        self.calls.append(("logs", tail))
        return self.logs_result

    def pull_model(self, role: Role, *, progress: bool = False) -> Result:
        self.calls.append(("pull", role, progress))
        return self.pull_results.get(role, OK)

    def start_native(self) -> Result:
        self.calls.append(("start_native",))
        return self.native_result

    def stop_native(self) -> Result:
        self.calls.append(("stop_native",))
        return OK

    def native_running(self) -> bool:
        return self.running.pop(0) if self.running else False


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WaddlePaths:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    return resolve_paths(os.environ, Path.home())


@pytest.fixture
def ctl(paths: WaddlePaths, monkeypatch: pytest.MonkeyPatch) -> FakeCtl:
    fake = FakeCtl(paths)
    monkeypatch.setattr(brain_cmd, "make_ctl", lambda _paths: fake)
    return fake


# ---- argument parsing ----------------------------------------------------------------------


def test_parser_accepts_the_brain_and_models_commands() -> None:
    parse = cli.build_parser().parse_args
    assert parse(["brain", "up"]).native is False
    assert parse(["brain", "up", "--native"]).native is True
    assert parse(["brain", "down"]).action == "down"
    assert parse(["brain", "status"]).action == "status"
    assert parse(["brain", "logs"]).tail == 200
    assert parse(["brain", "logs", "--tail", "5"]).tail == 5
    assert parse(["models", "pull", "embedder"]).role == "embedder"
    assert parse(["models", "status"]).action == "status"


@pytest.mark.parametrize(
    "argv",
    [
        ["brain"],
        ["brain", "restart"],
        ["brain", "down", "--native"],
        ["brain", "logs", "--tail", "0"],
        ["brain", "logs", "--tail", "1001"],
        ["brain", "logs", "--tail", "many"],
        ["brain", "up", "extra"],
        ["models"],
        ["models", "pull"],
        ["models", "pull", "embedder", "smart_model"],
        ["models", "rm", "embedder"],
    ],
)
def test_bad_usage_exits_2_and_touches_nothing(argv: list[str], ctl: FakeCtl) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2
    assert ctl.calls == []


# ---- brain up / down / status / logs -------------------------------------------------------


def test_up_creates_the_token_first_then_starts(
    paths: WaddlePaths, ctl: FakeCtl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not paths.link_token.exists()
    assert cli.main(["brain", "up"]) == 0
    assert ctl.calls == [("up",)]
    assert ctl.token_seen is True, "the token must exist before BrainCtl.up() is called"
    assert stat.S_IMODE(paths.link_token.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "waddle models pull all" in out and "waddle brain status" in out
    assert paths.link_token.read_text().strip() not in out


@pytest.mark.parametrize(("result", "code"), [(FAILED, 1), (UNAVAILABLE, 2)])
def test_up_failure_uses_brainctl_message_and_exit_code(
    result: Result, code: int, ctl: FakeCtl, capsys: pytest.CaptureFixture[str]
) -> None:
    ctl.up_result = result
    assert cli.main(["brain", "up"]) == code
    captured = capsys.readouterr()
    assert result.reason in captured.err
    assert "containers are started" not in captured.out


def test_up_with_an_untrusted_token_file_stops_before_docker(
    paths: WaddlePaths, ctl: FakeCtl, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.data_dir.mkdir(parents=True)
    paths.link_token.write_text(TOKEN)
    paths.link_token.chmod(0o644)  # readable by others: not ours to trust
    assert cli.main(["brain", "up"]) == 2
    assert ctl.calls == []
    captured = capsys.readouterr()
    assert "link token" in captured.err and TOKEN not in captured.err + captured.out


def test_down_says_models_are_kept(ctl: FakeCtl, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["brain", "down"]) == 0
    assert ctl.calls == [("down",)]
    assert "models are kept" in capsys.readouterr().out
    ctl.down_result = FAILED
    assert cli.main(["brain", "down"]) == 1
    assert FAILED.reason in capsys.readouterr().err


def test_status_lists_each_service(ctl: FakeCtl, capsys: pytest.CaptureFixture[str]) -> None:
    ctl.status_result = Status(
        Outcome.OK,
        "",
        (ServiceStatus("waddle-api", "running", "healthy"), ServiceStatus("ollama", "running", "")),
    )
    assert cli.main(["brain", "status"]) == 0
    out = capsys.readouterr().out
    assert "waddle-api: running (healthy)" in out and "ollama: running" in out
    assert "waddle brain up" not in out


def test_status_is_1_when_a_service_is_missing_or_stopped(
    ctl: FakeCtl, capsys: pytest.CaptureFixture[str]
) -> None:
    ctl.status_result = Status(Outcome.OK, "", (ServiceStatus("ollama", "running", ""),))
    assert cli.main(["brain", "status"]) == 1
    out = capsys.readouterr().out
    assert "waddle-api: not started" in out and "waddle brain up" in out
    ctl.status_result = Status(
        Outcome.OK,
        "",
        (ServiceStatus("waddle-api", "exited", ""), ServiceStatus("ollama", "running", "")),
    )
    assert cli.main(["brain", "status"]) == 1
    assert "waddle-api: exited" in capsys.readouterr().out


def test_status_unavailable_is_2_with_the_reason(
    ctl: FakeCtl, capsys: pytest.CaptureFixture[str]
) -> None:
    ctl.status_result = Status(Outcome.UNAVAILABLE, "Docker is not installed.")
    assert cli.main(["brain", "status"]) == 2
    captured = capsys.readouterr()
    assert "Docker is not installed." in captured.err
    assert "not started" not in captured.out, "an unknown state must not be shown as stopped"


def test_logs_prints_brainctl_text_and_passes_the_tail(
    ctl: FakeCtl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["brain", "logs", "--tail", "7"]) == 0
    assert ctl.calls == [("logs", 7)]
    assert capsys.readouterr().out == "line one\nline two\n"
    ctl.logs_result = Logs(Outcome.FAILED, "Docker reported an error.", "")
    assert cli.main(["brain", "logs"]) == 1
    assert "Docker reported an error." in capsys.readouterr().err


# ---- native foreground mode ----------------------------------------------------------------


def test_native_says_what_it_gives_up_before_starting(
    paths: WaddlePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCtl(paths)
    assert brain_cmd.up_native(paths, fake, sleep=lambda _s: None) == 1  # exits at once
    out = capsys.readouterr().out
    for phrase in ("network boundary", "file boundary", "Ollama you already run"):
        assert phrase in out
    assert paths.link_token.is_file(), "native mode needs the token too"


def test_native_stops_the_child_on_ctrl_c(
    paths: WaddlePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCtl(paths)
    fake.running = [True] * 50

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    assert brain_cmd.up_native(paths, fake, sleep=interrupted) == 0
    assert fake.calls == [("start_native",), ("stop_native",)]
    assert "The brain is stopped." in capsys.readouterr().out


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_native_stops_the_child_on_a_real_signal_and_restores_handlers(
    signum: signal.Signals, paths: WaddlePaths
) -> None:
    fake = FakeCtl(paths)
    fake.running = [True] * 50
    before = signal.getsignal(signum)

    def send(_seconds: float) -> None:
        os.kill(os.getpid(), signum)

    assert brain_cmd.up_native(paths, fake, sleep=send) == 0
    assert fake.calls == [("start_native",), ("stop_native",)]
    assert len(fake.running) > 40, "the wait must end at the signal, not run on"
    assert signal.getsignal(signum) is before


def test_native_reports_a_child_that_exits_by_itself(
    paths: WaddlePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCtl(paths)
    fake.running = [True, True, False]
    slept: list[float] = []
    assert brain_cmd.up_native(paths, fake, sleep=slept.append) == 1
    assert len(slept) == 2
    assert fake.calls == [("start_native",), ("stop_native",)], "stop cleans up its config"
    captured = capsys.readouterr()
    assert "stopped by itself" in captured.err and "The brain is stopped." not in captured.out


@pytest.mark.parametrize(("result", "code"), [(FAILED, 1), (UNAVAILABLE, 2)])
def test_native_start_failure(
    result: Result, code: int, paths: WaddlePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCtl(paths)
    fake.native_result = result
    fake.running = [True] * 5
    assert brain_cmd.up_native(paths, fake, sleep=lambda _s: None) == code
    assert result.reason in capsys.readouterr().err
    assert len(fake.running) == 5, "nothing to wait for"


def test_native_with_a_bad_token_starts_nothing(paths: WaddlePaths) -> None:
    paths.data_dir.mkdir(parents=True)
    paths.link_token.mkdir()
    fake = FakeCtl(paths)
    assert brain_cmd.up_native(paths, fake) == 2
    assert ("start_native",) not in fake.calls


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_native_with_the_real_brainctl_leaves_no_child_behind(
    paths: WaddlePaths, tmp_path: Path
) -> None:
    """The real controller and a real child process: Ctrl-C must end the child."""
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "fake_brain_program.py"
    script.write_text(
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    real = BrainCtl(paths, brain_program=[sys.executable, "-I", str(script)])
    pids: list[int] = []

    def ctrl_c(_seconds: float) -> None:
        pids.append(int(pid_file.read_text()))
        assert _alive(pids[0])
        os.kill(os.getpid(), signal.SIGINT)

    assert brain_cmd.up_native(paths, real, sleep=ctrl_c) == 0
    assert pids and not real.native_running()
    deadline = time.monotonic() + 5
    while _alive(pids[0]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pids[0])
    assert not (paths.runtime_dir / compose.NATIVE_CONFIG_NAME).exists()


def test_native_through_main(
    paths: WaddlePaths, ctl: FakeCtl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["brain", "up", "--native"]) == 1  # the stand-in child is never running
    assert ctl.calls == [("start_native",), ("stop_native",)]
    assert ("up",) not in ctl.calls


# ---- models pull ---------------------------------------------------------------------------


def test_pull_one_role_shows_the_configured_name(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeCtl()
    assert models_cmd.pull(fake, "embedder", lambda: NAMES) == 0
    assert fake.calls == [("pull", "embedder", True)]
    out = capsys.readouterr().out
    assert "embedder: downloading emb-x:3" in out
    assert "GB" not in out and "MB" not in out, "sizes come from the download, not from us"


def test_pull_all_pulls_every_role_in_order() -> None:
    fake = FakeCtl()
    assert models_cmd.pull(fake, "all", lambda: NAMES) == 0
    assert fake.calls == [
        ("pull", "fast_vlm", True),
        ("pull", "smart_model", True),
        ("pull", "embedder", True),
    ]


@pytest.mark.parametrize(
    "name", ["fast-x:1", "qwen3.5:9b", "llama3", "", "ALL", "fast_vlm ", "--help-me", "../x"]
)
def test_pull_refuses_anything_that_is_not_a_role(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCtl()
    assert models_cmd.pull(fake, name, lambda: NAMES) == 2
    assert fake.calls == []
    assert "by role only" in capsys.readouterr().err


def test_pull_of_a_model_name_through_main_is_refused(
    ctl: FakeCtl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["models", "pull", "qwen3.5:9b"]) == 2
    assert ctl.calls == []
    assert cli.main(["models", "pull", "embedder"]) == 0  # the shipped brain.toml names it
    assert ctl.calls == [("pull", "embedder", True)]
    assert compose.configured_models()["embedder"] in capsys.readouterr().out


@pytest.mark.parametrize(("result", "code"), [(FAILED, 1), (UNAVAILABLE, 2)])
def test_pull_all_stops_at_the_first_failure(
    result: Result, code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCtl()
    fake.pull_results["smart_model"] = result
    assert models_cmd.pull(fake, "all", lambda: NAMES) == code
    assert [call[1] for call in fake.calls] == ["fast_vlm", "smart_model"]
    captured = capsys.readouterr()
    assert result.reason in captured.err
    assert "Not downloaded: smart_model, embedder" in captured.out


def test_pull_with_a_broken_config_or_a_missing_role(capsys: pytest.CaptureFixture[str]) -> None:
    def broken() -> dict[Role, str]:
        raise ComposeError("x")

    fake = FakeCtl()
    assert models_cmd.pull(fake, "all", broken) == 2
    assert models_cmd.pull(fake, "embedder", lambda: {"fast_vlm": "fast-x:1"}) == 1
    assert fake.calls == []
    assert "names no model" in capsys.readouterr().err


# ---- models status -------------------------------------------------------------------------


def _models(**available: tuple[bool, bool]) -> tuple[ModelStatus, ...]:
    return tuple(
        ModelStatus(role=role, model="as-the-brain-says", available=a, loaded=b)  # type: ignore[arg-type]
        for role, (a, b) in available.items()
    )


def test_models_status_all_present(paths: WaddlePaths, capsys: pytest.CaptureFixture[str]) -> None:
    seen = _models(fast_vlm=(True, True), smart_model=(True, False), embedder=(True, False))
    assert models_cmd.status(paths, lambda: NAMES, lambda _p: Probe(True, models=seen)) == 0
    out = capsys.readouterr().out
    assert "fast_vlm: fast-x:1: present, loaded" in out
    assert "smart_model: smart-x:2: present\n" in out


def test_models_status_missing_model_is_1(
    paths: WaddlePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _models(fast_vlm=(True, False), smart_model=(False, False), embedder=(True, False))
    assert models_cmd.status(paths, lambda: NAMES, lambda _p: Probe(True, models=seen)) == 1
    out = capsys.readouterr().out
    assert "smart_model: smart-x:2: not downloaded" in out and "waddle models pull all" in out


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("no_socket", "the brain is not running"),
        ("busy", "in use by the running Waddle"),
        ("bad_token", "refused Waddle's token"),
        ("something_new", "the brain did not answer"),
    ],
)
def test_models_status_says_not_checked_when_the_brain_cannot_be_asked(
    reason: str, words: str, paths: WaddlePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert models_cmd.status(paths, lambda: NAMES, lambda _p: Probe(False, reason)) == 2
    out = capsys.readouterr().out
    assert out.count("not checked") == 3 and words in out
    assert "present" not in out and "not downloaded" not in out


def test_models_status_role_the_brain_did_not_report(
    paths: WaddlePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _models(fast_vlm=(True, False))
    assert models_cmd.status(paths, lambda: NAMES, lambda _p: Probe(True, models=seen)) == 2
    assert capsys.readouterr().out.count("not checked") == 2


# ---- the probe, against a real socket ------------------------------------------------------


@pytest.fixture
def short_paths() -> Iterator[WaddlePaths]:
    root = Path(tempfile.mkdtemp(prefix="wd-", dir="/tmp"))  # socket paths must stay short
    made = WaddlePaths(root / "c", root / "d", root / "k", root / "run")
    made.data_dir.mkdir()
    made.runtime_dir.mkdir(mode=0o700)
    made.link_token.write_text(TOKEN)
    made.link_token.chmod(0o600)
    yield made
    shutil.rmtree(root, ignore_errors=True)


async def test_probe_reads_the_welcome_and_sends_nothing_else(short_paths: WaddlePaths) -> None:
    brain = fb.FakeBrain(short_paths.brain_socket, TOKEN)
    await brain.start()
    try:
        answer = await models_cmd.probe_brain(short_paths)
        await asyncio.sleep(0.05)
    finally:
        await brain.stop()
    assert answer.up and answer.models == fb.MODELS
    assert brain.connections == 1 and brain.received == []


async def test_probe_reports_why_it_could_not_ask(short_paths: WaddlePaths) -> None:
    assert await models_cmd.probe_brain(short_paths) == Probe(False, "no_socket")
    for handshake, token, reason in (
        ("refuse", TOKEN, "busy"),
        ("welcome", "cd" * 32, "bad_token"),
    ):
        brain = fb.FakeBrain(short_paths.brain_socket, token, handshake=handshake)  # type: ignore[arg-type]
        await brain.start()
        try:
            assert await models_cmd.probe_brain(short_paths) == Probe(False, reason)
        finally:
            await brain.stop()


async def test_probe_gives_up_on_a_silent_brain(short_paths: WaddlePaths) -> None:
    brain = fb.FakeBrain(short_paths.brain_socket, TOKEN, handshake="silent")
    await brain.start()
    try:
        started = time.monotonic()
        answer = await models_cmd.probe_brain(short_paths, seconds=0.3)
    finally:
        await brain.stop()
    assert answer == Probe(False, "timeout") and time.monotonic() - started < 3


# ---- install.sh / uninstall.sh -------------------------------------------------------------


def _commands(script: Path) -> list[str]:
    """Lines that do something: not comments, not text that is only printed."""
    lines = [line.strip() for line in script.read_text().splitlines()]
    return [line for line in lines if line and not line.startswith(("#", "echo "))]


def test_install_prints_the_next_steps_and_never_runs_docker() -> None:
    script = REPO / "install.sh"
    printed = [line for line in script.read_text().splitlines() if line.startswith("echo ")]
    steps = [
        next(i for i, line in enumerate(printed) if f"$WADDLE_BIN {step}" in line)
        for step in ("doctor  ", "brain up", "models pull all")
    ]
    assert steps == sorted(steps) and steps[0] > printed.index(
        'echo "Check:    $WADDLE_BIN doctor"'
    )
    for line in _commands(script):
        assert "docker" not in line and "models pull" not in line and "brain up" not in line


def test_uninstall_never_runs_docker_itself() -> None:
    for line in _commands(REPO / "uninstall.sh"):
        assert "docker" not in line, line


@pytest.mark.parametrize("purge", [False, True])
def test_uninstall_removes_the_link_files_and_explains_the_rest(purge: bool) -> None:
    root = Path(tempfile.mkdtemp(prefix="wd-", dir="/tmp"))
    try:
        home, run = root / "home", root / "run"
        data = home / ".local" / "share" / "waddle"
        data.mkdir(parents=True)
        (run / "waddle").mkdir(parents=True)
        (data / "link-token").write_text(TOKEN)
        (data / "desktop.db").write_text("kept")
        (run / "waddle" / "brain.native.toml").write_text("x")
        sock = run / "waddle" / "brain.sock"
        import socket

        server = socket.socket(socket.AF_UNIX)
        server.bind(str(sock))
        server.close()
        other = run / "not-waddle.sock"
        other.write_text("someone else's")
        script = root / "uninstall.sh"  # a copy: no venv next to it, so no waddle is run
        shutil.copy(REPO / "uninstall.sh", script)
        env = {"HOME": str(home), "XDG_RUNTIME_DIR": str(run), "PATH": "/usr/bin:/bin"}
        done = subprocess.run(
            ["/bin/bash", str(script), *(["--purge"] if purge else [])],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert not (data / "link-token").exists() and not sock.exists()
        assert not (run / "waddle").exists()
        assert other.exists()
        assert (data / "desktop.db").exists() is not purge
        assert "docker compose -p waddle down" in done.stdout
        assert "docker volume rm waddle_ollama-models" in done.stdout
    finally:
        shutil.rmtree(root, ignore_errors=True)
