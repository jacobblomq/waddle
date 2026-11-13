"""``waddle doctor``, Phase 3 rows: Docker, GPU, the brain link, models, sandbox, app packs."""

from __future__ import annotations

import asyncio
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fake_brain as fb

from waddle_core.link import ModelStatus, Role
from waddle_core.paths import WaddlePaths
from waddle_desktop.brainctl import compose
from waddle_desktop.brainctl.compose import ComposeError, Outcome, ServiceStatus, Status
from waddle_desktop.cli import doctor
from waddle_desktop.cli.doctor import BAD, NOTE, OK, SKIP, WARN, Check
from waddle_desktop.cli.models_cmd import Probe

REPO = Path(__file__).resolve().parents[3]
TOKEN = "ab" * 32
NAMES: dict[Role, str] = {"fast_vlm": "fast-x:1", "smart_model": "smart-x:2", "embedder": "emb-x:3"}


class FakeCtl:
    def __init__(self, status: Status) -> None:
        self._status = status
        self.calls = 0

    def status(self) -> Status:
        self.calls += 1
        return self._status


def levels(checks: list[Check]) -> dict[str, str]:
    return {check.text: check.level for check in checks}


@pytest.fixture
def paths() -> Iterator[WaddlePaths]:
    root = Path(tempfile.mkdtemp(prefix="wd-", dir="/tmp"))  # socket paths must stay short
    made = WaddlePaths(root / "c", root / "d", root / "k", root / "run")
    made.data_dir.mkdir()
    yield made
    shutil.rmtree(root, ignore_errors=True)


def with_token(paths: WaddlePaths, mode: int = 0o600) -> None:
    paths.link_token.write_text(TOKEN)
    paths.link_token.chmod(mode)


# ---- docker --------------------------------------------------------------------------------


def test_docker_missing_is_a_problem_and_the_rest_is_not_checked() -> None:
    ctl = FakeCtl(Status(Outcome.OK))
    found = levels(doctor.check_docker(ctl, installed=False))
    assert found == {
        "Docker is not installed": BAD,
        "Docker service: not checked": SKIP,
        "Docker Compose plugin: not checked": SKIP,
        "brain containers: not checked": SKIP,
    }
    assert ctl.calls == 0


def test_docker_working_lists_the_services() -> None:
    services = (
        ServiceStatus("waddle-api", "running", "healthy"),
        ServiceStatus("ollama", "exited", ""),
    )
    found = levels(doctor.check_docker(FakeCtl(Status(Outcome.OK, "", services)), installed=True))
    assert found == {
        "Docker is installed": OK,
        "Docker service answers": OK,
        "Docker Compose plugin works": OK,
        "waddle-api: running (healthy)": OK,
        "ollama: exited": WARN,
    }


def test_docker_working_but_brain_not_started() -> None:
    checks = doctor.check_docker(FakeCtl(Status(Outcome.OK)), installed=True)
    found = levels(checks)
    assert found["waddle-api: not started"] == WARN and found["ollama: not started"] == WARN
    assert all("waddle brain up" in c.fix for c in checks if c.level == WARN)


def test_an_unhealthy_running_service_is_not_ok() -> None:
    services = (ServiceStatus("waddle-api", "running", "unhealthy"),)
    found = levels(doctor.check_docker(FakeCtl(Status(Outcome.OK, "", services)), installed=True))
    assert found["waddle-api: running (unhealthy)"] == WARN


def _failure(stderr: str) -> Status:
    result = compose.classify_failure(stderr)
    return Status(result.outcome, result.reason)


def test_docker_daemon_down() -> None:
    status = _failure("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")
    found = levels(doctor.check_docker(FakeCtl(status), installed=True))
    assert found == {
        "Docker is installed": OK,
        "Docker service is not running": BAD,
        "Docker Compose plugin: not checked": SKIP,
        "brain containers: not checked": SKIP,
    }


def test_docker_not_allowed_and_compose_missing() -> None:
    denied = _failure("permission denied while trying to connect to the Docker daemon socket")
    found = levels(doctor.check_docker(FakeCtl(denied), installed=True))
    assert found["your user is not allowed to use Docker"] == BAD
    assert found["Docker Compose plugin: not checked"] == SKIP
    plugin = _failure("docker: 'compose' is not a docker command.")
    found = levels(doctor.check_docker(FakeCtl(plugin), installed=True))
    assert found["Docker Compose plugin is not installed"] == BAD
    assert found["Docker service: not checked"] == SKIP
    assert "Docker service answers" not in found


def test_docker_unknown_failure_claims_nothing() -> None:
    found = levels(doctor.check_docker(FakeCtl(_failure("boom")), installed=True))
    assert found == {
        "Docker is installed": OK,
        "Docker reported an error.": BAD,
        "Docker service: not checked": SKIP,
        "Docker Compose plugin: not checked": SKIP,
        "brain containers: not checked": SKIP,
    }


def test_the_failures_doctor_tells_apart_have_their_own_sentences() -> None:
    known = {doctor._DAEMON_DOWN, doctor._NOT_ALLOWED, doctor._NO_COMPOSE}
    assert len(known) == 3 and compose.classify_failure("boom").reason not in known


# ---- gpu -----------------------------------------------------------------------------------


def test_gpu_driver_version_is_read_from_the_file(tmp_path: Path) -> None:
    version = tmp_path / "version"
    version.write_text(
        "NVRM version: NVIDIA UNIX x86_64 Kernel Module  580.95.05  Tue Sep 23 10:11:12 UTC 2025\n"
        "GCC version:  gcc version 15.2.0 (Ubuntu 15.2.0-4ubuntu4)\n"
    )
    found = levels(doctor.check_gpu(version))
    assert found == {
        "NVIDIA driver 580.95.05 is loaded": OK,
        "NVIDIA container runtime: not checked": SKIP,
    }
    version.write_text("something this code has never seen\n")
    assert levels(doctor.check_gpu(version))["NVIDIA driver is loaded"] == OK


def test_gpu_driver_absent_or_unreadable(tmp_path: Path) -> None:
    found = levels(doctor.check_gpu(tmp_path / "nope"))
    assert found["NVIDIA driver is not loaded"] == WARN
    assert found["NVIDIA container runtime: not checked"] == SKIP
    found = levels(doctor.check_gpu(tmp_path))  # a directory: cannot be read as a file
    assert found["NVIDIA driver: not checked (its version file cannot be read)"] == SKIP


# ---- brain link ----------------------------------------------------------------------------


def test_token_rows(paths: WaddlePaths) -> None:
    ok, checks = doctor.check_token(paths)
    assert not ok and levels(checks) == {"no link token yet": WARN}
    with_token(paths)
    ok, checks = doctor.check_token(paths)
    assert ok and levels(checks) == {"link token is present and private (mode 600)": OK}
    with_token(paths, 0o400)
    ok, checks = doctor.check_token(paths)
    assert ok and checks[0].level == WARN and "400" in checks[0].text
    with_token(paths, 0o644)
    ok, checks = doctor.check_token(paths)
    assert not ok and checks[0].level == BAD
    with_token(paths)
    paths.link_token.write_text("not a token")
    ok, checks = doctor.check_token(paths)
    assert not ok and checks[0].level == BAD
    assert all(TOKEN not in c.text + c.fix for c in checks)


async def _never(_paths: WaddlePaths) -> Probe:
    raise AssertionError("the token must not be sent")


async def test_brain_without_a_socket_is_not_probed(paths: WaddlePaths) -> None:
    with_token(paths)
    probe, checks = await doctor.check_brain(paths, _never)
    found = levels(checks)
    assert probe is None
    assert found["no brain socket: the brain is not running"] == WARN
    assert found["brain answers: not checked"] == SKIP


async def test_brain_with_an_unsafe_socket_is_not_probed(paths: WaddlePaths) -> None:
    with_token(paths)
    paths.runtime_dir.mkdir(mode=0o755)  # others could swap the socket
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(paths.brain_socket))
    try:
        probe, checks = await doctor.check_brain(paths, _never)
    finally:
        server.close()
    found = levels(checks)
    assert probe is None and found["brain socket is not safe to use"] == BAD
    assert found["brain answers: not checked"] == SKIP


async def test_brain_without_a_token_is_not_probed(paths: WaddlePaths) -> None:
    paths.runtime_dir.mkdir(mode=0o700)
    brain = fb.FakeBrain(paths.brain_socket, TOKEN)
    await brain.start()
    try:
        probe, checks = await doctor.check_brain(paths, _never)
    finally:
        await brain.stop()
    found = levels(checks)
    assert probe is None and found["brain socket is present and private"] == OK
    assert found["brain answers: not checked"] == SKIP and brain.connections == 0


@pytest.mark.parametrize(
    ("handshake", "token", "text", "level"),
    [
        ("welcome", TOKEN, "brain answers", OK),
        ("refuse", TOKEN, "brain answers (it is in use by the running Waddle)", OK),
        ("welcome", "cd" * 32, "brain refused Waddle's token", BAD),
        ("garbage", TOKEN, "brain did not answer (the brain did not answer)", BAD),
    ],
)
async def test_brain_probe_rows_against_a_real_socket(
    handshake: fb.Handshake, token: str, text: str, level: str, paths: WaddlePaths
) -> None:
    with_token(paths)
    paths.runtime_dir.mkdir(mode=0o700)
    brain = fb.FakeBrain(paths.brain_socket, token, handshake=handshake)
    await brain.start()
    try:
        probe, checks = await doctor.check_brain(paths)
    finally:
        await brain.stop()
    assert probe is not None and levels(checks)[text] == level
    assert probe.up is (text == "brain answers")


async def test_native_mode_is_called_out(paths: WaddlePaths) -> None:
    paths.runtime_dir.mkdir(mode=0o700)
    _, checks = await doctor.check_brain(paths, _never)
    assert not any("native" in c.text for c in checks)
    (paths.runtime_dir / compose.NATIVE_CONFIG_NAME).write_text("x")
    _, checks = await doctor.check_brain(paths, _never)
    row = next(c for c in checks if "native" in c.text)
    assert row.level == WARN and "no network or file boundary" in row.fix


# ---- models --------------------------------------------------------------------------------


def _seen(**roles: tuple[bool, bool]) -> tuple[ModelStatus, ...]:
    return tuple(
        ModelStatus(role=role, model="m", available=a, loaded=b)  # type: ignore[arg-type]
        for role, (a, b) in roles.items()
    )


def test_models_are_not_checked_without_a_probe() -> None:
    for probe in (None, Probe(False, "busy"), Probe(False, "connect_failed")):
        checks = doctor.check_models(probe, lambda: NAMES)
        assert [c.level for c in checks] == [SKIP, SKIP, SKIP]
        assert all("not checked" in c.text for c in checks)


def test_models_rows_from_what_the_brain_reported() -> None:
    probe = Probe(True, models=_seen(fast_vlm=(True, True), smart_model=(False, False)))
    checks = doctor.check_models(probe, lambda: NAMES)
    assert levels(checks) == {
        "fast_vlm: fast-x:1: present, loaded": OK,
        "smart_model: smart-x:2: not downloaded": WARN,
        "embedder: emb-x:3: not checked (the brain did not report this role)": SKIP,
    }
    assert checks[1].fix == "waddle models pull smart_model"


def test_models_with_a_broken_config() -> None:
    def broken() -> dict[Role, str]:
        raise ComposeError("x")

    checks = doctor.check_models(Probe(True), broken)
    assert len(checks) == 1 and checks[0].level == BAD


# ---- sandbox -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sandbox:
    available: bool
    reason: str


async def test_sandbox_rows() -> None:
    async def works() -> Sandbox:
        return Sandbox(True, "ok")

    async def missing() -> Sandbox:
        return Sandbox(False, "bwrap is not installed")

    async def broken() -> Sandbox:
        raise RuntimeError("boom")

    checks = await doctor.check_sandbox(works, lambda tool: tool == "man")
    assert levels(checks) == {
        "sandbox works (bubblewrap + prlimit)": OK,
        "bubblewrap version: not checked (0.8 or newer is needed)": SKIP,
        "man: found (optional, from man-db)": NOTE,
        "tldr: not found (optional, from a tldr client)": NOTE,
    }
    checks = await doctor.check_sandbox(missing, lambda _tool: False)
    assert checks[0].level == WARN and "bwrap is not installed" in checks[0].text
    assert "bubblewrap 0.8" in checks[0].fix and "util-linux" in checks[0].fix
    checks = await doctor.check_sandbox(broken, lambda _tool: False)
    assert checks[0].level == SKIP and "not checked" in checks[0].text


# ---- app packs -----------------------------------------------------------------------------


def test_shipped_packs_all_load() -> None:
    shipped = len(list((REPO / "knowledge" / "apps").glob("*.toml")))
    checks = doctor.check_packs()
    assert shipped > 0 and levels(checks) == {f"{shipped} app packs loaded": OK}


def test_pack_errors_are_listed(tmp_path: Path) -> None:
    shutil.copy(REPO / "knowledge" / "apps" / "blender.toml", tmp_path / "blender.toml")
    (tmp_path / "broken.toml").write_text("id = [\n")
    checks = doctor.check_packs(tmp_path)
    assert levels(checks) == {
        "1 app packs loaded": OK,
        "broken.toml: skipped (not valid TOML)": WARN,
    }
    checks = doctor.check_packs(tmp_path / "missing")
    assert checks[0] == Check("apps", BAD, "no app packs loaded", "Reinstall Waddle.")
    assert checks[1].level == WARN and "cannot be read" in checks[1].text


# ---- the whole report ----------------------------------------------------------------------


def test_render_counts_what_was_not_checked() -> None:
    text = doctor.render([Check("gpu", OK, "fine"), Check("gpu", NOTE, "by the way")])
    assert text.endswith("All good.") and "- by the way" in text
    text = doctor.render([Check("gpu", OK, "fine"), Check("gpu", SKIP, "x: not checked")])
    assert "? x: not checked" in text and "All good." not in text
    assert text.endswith("0 problem(s), 0 warning(s), 1 not checked.")
    assert doctor.render([Check("gpu", BAD, "x")]).endswith("1 problem(s), 0 warning(s).")


def test_render_keeps_control_characters_and_line_breaks_out_of_the_terminal() -> None:
    hostile = "taken by \x1b[2J\x1b]0;x\x07Evil\n✓ all fine\u202e"
    text = doctor.render([Check("hotkeys", WARN, hostile, "fix \x1b[31m\r\nnow")])
    assert "\x1b" not in text and "\x07" not in text and "\u202e" not in text and "\r" not in text
    assert text.splitlines()[1].startswith("  ! taken by ") and "✓ all fine" in text.splitlines()[1]
    assert text.splitlines()[2] == "      → fix [31m now"
    assert len(doctor.render([Check("apps", WARN, "x" * 5000)]).splitlines()[1]) < 500


def test_collect_has_every_new_area_and_uses_the_given_ctl(
    paths: WaddlePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "docker_installed", lambda: True)
    ctl = FakeCtl(_failure("cannot connect to the docker daemon"))
    checks = asyncio.run(doctor.collect_phase3(paths, ctl))
    assert ctl.calls == 1
    assert [a for a in doctor._ORDER if a in {c.area for c in checks}] == [
        "docker",
        "gpu",
        "brain",
        "models",
        "sandbox",
        "apps",
    ]
    found = levels(checks)
    assert found["Docker service is not running"] == BAD
    assert found["brain answers: not checked"] == SKIP
    assert sum(1 for c in checks if c.area == "models" and c.level == SKIP) == 3
    assert {c.area for c in checks} <= set(doctor._ORDER), "run() sorts by this list"
