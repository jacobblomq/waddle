"""brainctl.compose: a closed list of docker calls on Waddle's own project (I2, §5.1)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from waddle_brain.config import RoleConfig, load_brain_config
from waddle_core.paths import WaddlePaths
from waddle_desktop import log
from waddle_desktop.brainctl import compose
from waddle_desktop.brainctl.compose import BrainCtl, ComposeError, Outcome, ServiceStatus

REPO = Path(__file__).resolve().parents[3]
SHIPPED = str(REPO / "deploy" / "docker-compose.yml")
HEAD = ["docker", "compose", "-p", "waddle", "-f", SHIPPED, "--env-file", "/dev/null"]
TOKEN = "ab" * 32
CANARY = "CANARY-what-the-user-asked"

FAKE_DOCKER = """\
import json, os, sys, time
record, out, err, code, delay = {params!r}
with open(record, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
time.sleep(delay)
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(code)
"""


@pytest.fixture
def paths(tmp_path: Path) -> WaddlePaths:
    made = WaddlePaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        runtime_dir=tmp_path / "run" / "waddle",
    )
    made.data_dir.mkdir()
    made.link_token.write_text(TOKEN + "\n")
    made.link_token.chmod(0o600)
    return made


class Docker:
    """A stand-in docker: records how it was called, then answers as told."""

    def __init__(
        self, tmp_path: Path, out: str = "", err: str = "", code: int = 0, delay: float = 0
    ):
        self.record = tmp_path / "docker-calls.jsonl"
        script = tmp_path / "fake_docker.py"
        script.write_text(FAKE_DOCKER.format(params=(str(self.record), out, err, code, delay)))
        self.prefix = [sys.executable, "-I", str(script)]

    def calls(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(log, "event", lambda name, **fields: lines.append(log.render(name, fields)))
    return lines


# --- argv builders -------------------------------------------------------------------------


def test_every_argv_is_fixed_and_names_the_shipped_file_and_project() -> None:
    assert compose.up_argv() == [*HEAD, "up", "--detach", "--build"]
    assert compose.down_argv() == [*HEAD, "down"]
    assert compose.ps_argv() == [*HEAD, "ps", "--all", "--format", "json"]
    assert compose.logs_argv(50) == [*HEAD, "logs", "--no-color", "--tail", "50"]
    assert compose.pull_argv("qwen3.5:9b") == [
        *HEAD,
        "--profile",
        "pull",
        "run",
        "--rm",
        "-T",
        "ollama-pull",
        "qwen3.5:9b",
    ]


def test_the_compose_file_is_pinned_to_the_one_in_this_checkout() -> None:
    assert Path(SHIPPED).is_file()
    assert Path(SHIPPED) == compose.COMPOSE_FILE
    assert compose.BRAIN_CONFIG == REPO / "deploy" / "brain.toml"
    assert compose.NATIVE_TEMPLATE == REPO / "deploy" / "brain.native.toml"


def test_down_never_removes_volumes_and_nothing_execs_into_a_container() -> None:
    every = [
        compose.up_argv(),
        compose.down_argv(),
        compose.ps_argv(),
        compose.logs_argv(1),
        compose.pull_argv("m"),
    ]
    for argv in every:
        assert argv[:8] == HEAD
        assert not {"-v", "--volumes", "--rmi", "exec", "cp", "prune", "rm", "kill"} & set(argv)


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "two words",
        "-leading-dash",
        "--rm",
        "name;rm",
        "name$(id)",
        "name`id`",
        "name|cat",
        "name&",
        "name>x",
        "name\nother",
        "name\n",
        "name'q",
        'name"q',
        "nämé",
        "a" * 129,
        None,
        7,
        ["qwen"],
    ],
)
def test_pull_rejects_anything_that_is_not_a_plain_model_name(name: Any) -> None:
    with pytest.raises(ComposeError):
        compose.pull_argv(name)


@pytest.mark.parametrize("name", ["qwen3.5:9b", "library/gemma4:e4b", "hf.co/org/model+x:Q4_K_M"])
def test_pull_accepts_real_model_names(name: str) -> None:
    assert compose.pull_argv(name)[-1] == name
    assert RoleConfig(model=name).model == name


def test_the_model_name_rule_is_the_brains_rule() -> None:
    field = RoleConfig.model_json_schema()["properties"]["model"]
    assert field["pattern"] == compose.MODEL_PATTERN
    assert field["maxLength"] == compose.MODEL_MAX_CHARS


@pytest.mark.parametrize("tail", [0, -1, 1001, True, "5", 2.0])
def test_logs_tail_must_be_a_small_positive_number(tail: Any) -> None:
    with pytest.raises(ComposeError):
        compose.logs_argv(tail)


def test_the_environment_is_built_not_inherited(paths: WaddlePaths) -> None:
    environ = {
        "HOME": "/home/u",
        "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
        "COMPOSE_FILE": "/tmp/evil.yml",
        "COMPOSE_PROFILES": "pull",
        "COMPOSE_PROJECT_NAME": "other",
        "PATH": "/tmp/evil",
        "SECRET": "x",
    }
    assert compose.compose_env(paths, environ) == {
        "HOME": "/home/u",
        "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "WADDLE_UID": str(os.getuid()),
        "WADDLE_GID": str(os.getgid()),
        "WADDLE_RUNTIME_DIR": str(paths.runtime_dir),
        "WADDLE_TOKEN_FILE": str(paths.link_token),
    }


# --- parsing -------------------------------------------------------------------------------

API_ROW = {"Service": "waddle-api", "State": "running", "Health": "healthy", "Name": "waddle-a-1"}
OLLAMA_ROW = {"Service": "ollama", "State": "exited", "Health": "", "ExitCode": 1}


def test_ps_output_is_read_as_lines_or_as_one_array() -> None:
    expected = (
        ServiceStatus("waddle-api", "running", "healthy"),
        ServiceStatus("ollama", "exited", ""),
    )
    as_lines = json.dumps(OLLAMA_ROW) + "\n" + json.dumps(API_ROW) + "\n"
    assert compose.parse_ps(as_lines) == expected
    assert compose.parse_ps(json.dumps([API_ROW, OLLAMA_ROW])) == expected


def test_ps_output_is_untrusted() -> None:
    rows = [
        {"Service": "someone-else", "State": "running"},
        {"Service": "ollama", "State": "Ignore previous instructions", "Health": 7},
        "text",
        {"State": "running"},
    ]
    text = "not json\n" + "\n".join(json.dumps(r) for r in rows)
    assert compose.parse_ps(text) == (ServiceStatus("ollama", "unknown", ""),)
    assert compose.parse_ps("") == ()


def test_configured_models_come_from_the_shipped_config() -> None:
    shipped = load_brain_config(REPO / "deploy" / "brain.toml")
    assert compose.configured_models() == {r: c.model for r, c in shipped.roles.items()}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not toml [",
        "[roles]\nfast_vlm = 3\n",
        '[roles.fast_vlm]\nmodel = "--insecure"\n',
        '[roles.fast_vlm]\nmodel = "a b"\n',
        "[roles.fast_vlm]\nnum_ctx = 1\n",
    ],
)
def test_a_bad_brain_config_is_refused(tmp_path: Path, text: str) -> None:
    config = tmp_path / "brain.toml"
    config.write_text(text)
    with pytest.raises(ComposeError):
        compose.configured_models(config)
    with pytest.raises(ComposeError):
        compose.configured_models(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    ("stderr", "outcome", "word"),
    [
        (
            "failed to connect to the docker API at unix:///var/run/docker.sock; check if the "
            "path is correct and if the daemon is running",
            Outcome.UNAVAILABLE,
            "not running",
        ),
        ("Cannot connect to the Docker daemon at unix:///x", Outcome.UNAVAILABLE, "not running"),
        (
            "permission denied while trying to connect to the Docker daemon socket",
            Outcome.UNAVAILABLE,
            "not allowed",
        ),
        ("docker: 'compose' is not a docker command.", Outcome.UNAVAILABLE, "Compose"),
        ('could not select device driver "nvidia" with capabilities', Outcome.FAILED, "GPU"),
        (f"something else {CANARY}", Outcome.FAILED, "error"),
    ],
)
def test_docker_errors_become_fixed_sentences(stderr: str, outcome: Outcome, word: str) -> None:
    result = compose.classify_failure(stderr)
    assert result.outcome is outcome
    assert word in result.reason
    assert CANARY not in result.reason


# --- the controller, against a stand-in docker ---------------------------------------------


def test_up_runs_the_fixed_argv_with_the_built_environment(
    paths: WaddlePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPOSE_PROFILES", "pull")
    docker = Docker(tmp_path)
    result = BrainCtl(paths, docker=docker.prefix).up()
    assert result.ok and result.reason == ""
    (call,) = docker.calls()
    assert call["argv"] == compose.up_argv()[1:]
    assert call["env"]["WADDLE_TOKEN_FILE"] == str(paths.link_token)
    assert call["env"]["WADDLE_RUNTIME_DIR"] == str(paths.runtime_dir)
    assert call["env"]["WADDLE_UID"] == str(os.getuid())
    assert not [name for name in call["env"] if name.startswith("COMPOSE")]
    assert stat.S_IMODE(paths.runtime_dir.stat().st_mode) == 0o700


def test_up_without_a_token_runs_nothing(paths: WaddlePaths, tmp_path: Path) -> None:
    paths.link_token.unlink()
    docker = Docker(tmp_path)
    result = BrainCtl(paths, docker=docker.prefix).up()
    assert result.outcome is Outcome.UNAVAILABLE
    assert "token" in result.reason
    assert docker.calls() == []


def test_up_without_the_shipped_brain_config_runs_nothing(
    paths: WaddlePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compose, "BRAIN_CONFIG", tmp_path / "missing.toml")
    docker = Docker(tmp_path)
    result = BrainCtl(paths, docker=docker.prefix).up()
    assert result.outcome is Outcome.UNAVAILABLE
    assert "brain config" in result.reason
    assert docker.calls() == []


def test_status_parses_what_docker_prints(paths: WaddlePaths, tmp_path: Path) -> None:
    docker = Docker(tmp_path, out=json.dumps(API_ROW) + "\n")
    status = BrainCtl(paths, docker=docker.prefix).status()
    assert status.ok
    assert status.services == (ServiceStatus("waddle-api", "running", "healthy"),)
    assert docker.calls()[0]["argv"] == compose.ps_argv()[1:]


def test_a_docker_error_is_a_result_not_an_exception(
    paths: WaddlePaths, tmp_path: Path, logged: list[str]
) -> None:
    docker = Docker(tmp_path, out="half", err=f"boom {CANARY}", code=17)
    ctl = BrainCtl(paths, docker=docker.prefix)
    status = ctl.status()
    assert status.outcome is Outcome.FAILED and status.services == ()
    assert ctl.down().outcome is Outcome.FAILED
    assert CANARY not in status.reason
    assert '"exit":17' in logged[0] and '"op":"status"' in logged[0]
    assert CANARY not in "".join(logged)


def test_a_stopped_daemon_is_unavailable(paths: WaddlePaths, tmp_path: Path) -> None:
    docker = Docker(tmp_path, err="failed to connect to the docker API at unix:///x", code=1)
    result = BrainCtl(paths, docker=docker.prefix).up()
    assert result.outcome is Outcome.UNAVAILABLE
    assert result.reason == "Docker is installed but its service is not running."


def test_the_real_docker_with_no_daemon_is_unavailable(paths: WaddlePaths) -> None:
    docker = shutil.which("docker")
    if docker is None or Path("/var/run/docker.sock").exists() or "DOCKER_HOST" in os.environ:
        pytest.skip("needs docker installed with no daemon")
    status = BrainCtl(paths, docker=[docker]).status()
    assert status.outcome is Outcome.UNAVAILABLE
    assert status.reason == "Docker is installed but its service is not running."


def test_docker_missing_is_unavailable(
    paths: WaddlePaths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(compose, "DOCKER_CANDIDATES", (str(tmp_path / "no-docker"),))
    ctl = BrainCtl(paths)
    for result in (ctl.up(), ctl.down(), ctl.status(), ctl.logs(), ctl.pull_model("embedder")):
        assert result.outcome is Outcome.UNAVAILABLE
        assert result.reason == "Docker is not installed."
    gone = BrainCtl(paths, docker=[str(tmp_path / "no-docker")]).status()
    assert gone.outcome is Outcome.UNAVAILABLE
    assert gone.reason == "Docker could not be started."


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory"])
def test_it_refuses_when_the_shipped_compose_file_is_not_a_plain_file(
    paths: WaddlePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    fake = tmp_path / "docker-compose.yml"
    if kind == "symlink":
        fake.symlink_to(SHIPPED)
    elif kind == "directory":
        fake.mkdir()
    monkeypatch.setattr(compose, "COMPOSE_FILE", fake)
    docker = Docker(tmp_path)
    ctl = BrainCtl(paths, docker=docker.prefix)
    for result in (ctl.up(), ctl.down(), ctl.status(), ctl.logs(), ctl.pull_model("embedder")):
        assert result.outcome is Outcome.UNAVAILABLE
        assert "compose file" in result.reason
    assert docker.calls() == []


def test_a_slow_docker_is_cut_off(
    paths: WaddlePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compose, "QUERY_SECONDS", 0.3)
    docker = Docker(tmp_path, delay=20)
    started = time.monotonic()
    status = BrainCtl(paths, docker=docker.prefix).status()
    assert time.monotonic() - started < 10
    assert status.outcome is Outcome.FAILED
    assert "in time" in status.reason


def test_logs_are_returned_cleaned_and_never_logged(
    paths: WaddlePaths, tmp_path: Path, logged: list[str]
) -> None:
    docker = Docker(tmp_path, out=f"waddle-api-1 | {CANARY}\x1b[31m\n", err="from stderr\n")
    logs = BrainCtl(paths, docker=docker.prefix).logs(tail=5000)
    assert logs.ok
    assert CANARY in logs.text and "from stderr" in logs.text
    assert "\x1b" not in logs.text
    assert docker.calls()[0]["argv"] == compose.logs_argv(1000)[1:]
    assert logged and CANARY not in "".join(logged)


def test_pull_takes_the_model_from_the_shipped_config_by_role(
    paths: WaddlePaths, tmp_path: Path
) -> None:
    docker = Docker(tmp_path)
    assert BrainCtl(paths, docker=docker.prefix).pull_model("fast_vlm").ok
    expected = load_brain_config(REPO / "deploy" / "brain.toml").roles["fast_vlm"].model
    assert docker.calls()[0]["argv"] == compose.pull_argv(expected)[1:]


@pytest.mark.parametrize("role", ["qwen3.5:9b", "", "--rm", "fast_vlm; id", None])
def test_pull_accepts_a_role_never_a_name(paths: WaddlePaths, tmp_path: Path, role: Any) -> None:
    docker = Docker(tmp_path)
    result = BrainCtl(paths, docker=docker.prefix).pull_model(role)
    assert result.outcome is Outcome.FAILED
    assert docker.calls() == []


def test_pull_refuses_a_config_with_a_bad_model_name(
    paths: WaddlePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "brain.toml"
    config.write_text('[roles.fast_vlm]\nmodel = "--entrypoint=sh"\n')
    monkeypatch.setattr(compose, "BRAIN_CONFIG", config)
    docker = Docker(tmp_path)
    result = BrainCtl(paths, docker=docker.prefix).pull_model("fast_vlm")
    assert result.outcome is Outcome.FAILED
    assert docker.calls() == []


def test_pull_with_progress_leaves_the_output_to_the_terminal(
    paths: WaddlePaths, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    docker = Docker(tmp_path, out="pulling 42%\n")
    assert BrainCtl(paths, docker=docker.prefix).pull_model("embedder", progress=True).ok
    assert "pulling 42%" in capfd.readouterr().out
    assert BrainCtl(paths, docker=docker.prefix).pull_model("embedder").ok
    assert capfd.readouterr().out == ""


# --- native mode ---------------------------------------------------------------------------

FAKE_BRAIN = """\
import os, pathlib, sys, time
pathlib.Path({seen!r}).write_text("\\n".join(sys.argv[1:]))
pathlib.Path({seen!r} + "-env").write_text("\\n".join(sorted(os.environ)))
time.sleep({seconds})
sys.exit({code})
"""


def fake_brain(tmp_path: Path, seconds: float, code: int = 0) -> tuple[list[str], Path]:
    seen = tmp_path / "brain-argv"
    script = tmp_path / "fake_brain.py"
    script.write_text(FAKE_BRAIN.format(seen=str(seen), seconds=seconds, code=code))
    return [sys.executable, "-I", str(script)], seen


def test_the_native_config_carries_this_users_paths(paths: WaddlePaths, tmp_path: Path) -> None:
    template = compose.NATIVE_TEMPLATE.read_text(encoding="utf-8")
    generated = tmp_path / "generated.toml"
    generated.write_text(compose.render_native_config(template, paths))
    config = load_brain_config(generated)
    assert config.socket_path == paths.brain_socket
    assert config.token_path == paths.link_token
    assert config.ollama_url == "http://127.0.0.1:11434"
    assert config.roles == load_brain_config(REPO / "deploy" / "brain.toml").roles


@pytest.mark.parametrize("bad", ["/run/it's/waddle", "/run/a\nsocket_path = '/x'", "relative"])
def test_a_path_that_could_break_the_config_is_refused(tmp_path: Path, bad: str) -> None:
    odd = WaddlePaths(tmp_path, tmp_path, tmp_path, runtime_dir=Path(bad))
    template = compose.NATIVE_TEMPLATE.read_text(encoding="utf-8")
    with pytest.raises(ComposeError):
        compose.render_native_config(template, odd)
    with pytest.raises(ComposeError):
        compose.render_native_config("socket_path = '/fixed'\ntoken_path = '/fixed'\n", odd)


def test_native_start_and_stop_own_one_child(paths: WaddlePaths, tmp_path: Path) -> None:
    program, seen = fake_brain(tmp_path, seconds=60)
    ctl = BrainCtl(paths, brain_program=program)
    assert not ctl.native_running()
    assert ctl.stop_native().ok  # nothing to stop is not an error
    assert ctl.start_native().ok
    try:
        assert ctl.native_running()
        config = paths.runtime_dir / "brain.native.toml"
        assert seen.read_text().splitlines() == ["--config", str(config)]
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert load_brain_config(config).socket_path == paths.brain_socket
        assert ctl.start_native().ok  # already running: no second child
    finally:
        started = time.monotonic()
        assert ctl.stop_native().ok
    assert time.monotonic() - started < 4
    assert not ctl.native_running()
    assert not config.exists()


def test_a_native_brain_that_exits_at_once_is_reported(paths: WaddlePaths, tmp_path: Path) -> None:
    program, _seen = fake_brain(tmp_path, seconds=0, code=2)
    ctl = BrainCtl(paths, brain_program=program)
    result = ctl.start_native()
    assert result.outcome is Outcome.FAILED
    assert "exit status 2" in result.reason
    assert not ctl.native_running()
    assert not (paths.runtime_dir / "brain.native.toml").exists()  # nothing left behind


def test_a_native_brain_that_cannot_be_started_leaves_no_config(
    paths: WaddlePaths, tmp_path: Path
) -> None:
    result = BrainCtl(paths, brain_program=[str(tmp_path / "nope")]).start_native()
    assert result.outcome is Outcome.UNAVAILABLE
    assert not (paths.runtime_dir / "brain.native.toml").exists()


def test_the_native_brain_gets_a_built_environment_not_ours(
    paths: WaddlePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WADDLE_TEST_CANARY", "x")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    program, seen = fake_brain(tmp_path, seconds=60)
    ctl = BrainCtl(paths, brain_program=program)
    try:
        assert ctl.start_native().ok
        names = set(Path(f"{seen}-env").read_text().splitlines())
    finally:
        ctl.stop_native()
    assert names - {"LC_CTYPE"} <= {"HOME", "LANG", "PATH"}


def test_native_mode_without_an_installed_brain_is_unavailable(
    paths: WaddlePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compose.sys, "executable", str(tmp_path / "bin" / "python"))
    result = BrainCtl(paths).start_native()
    assert result.outcome is Outcome.UNAVAILABLE
    assert BrainCtl(paths, brain_program=[str(tmp_path / "nope")]).start_native().outcome is (
        Outcome.UNAVAILABLE
    )


def test_the_real_brain_starts_natively_and_serves_its_socket(paths: WaddlePaths) -> None:
    if not (Path(sys.executable).parent / "waddle-brain").exists():
        pytest.skip("waddle-brain is not installed in this environment")
    ctl = BrainCtl(paths)
    result = ctl.start_native()
    try:
        assert result.ok, result.reason
        deadline = time.monotonic() + 10
        while not paths.brain_socket.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stat.S_ISSOCK(paths.brain_socket.stat().st_mode)
        assert ctl.native_running()
    finally:
        assert ctl.stop_native().ok
    assert not ctl.native_running()
