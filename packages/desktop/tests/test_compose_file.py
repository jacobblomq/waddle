"""The shipped deployment files, checked rule by rule (ARCHITECTURE §5.1, I6, I8, D-019)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy"
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]
DOCKERFILE = (DEPLOY / "Dockerfile.api").read_text(encoding="utf-8")
PINNED_OLLAMA = re.compile(r"ollama/ollama:\d+\.\d+\.\d+")
QUIET_OLLAMA = {"OLLAMA_DEBUG": "0", "OLLAMA_DEBUG_LOG_REQUESTS": "0", "OLLAMA_NO_CLOUD": "1"}
NO_NEW_PRIVILEGES = ["no-new-privileges:true"]


def networks_of(service: dict[str, Any]) -> list[str]:
    return list(service.get("networks", ["default"]))


def test_only_this_phases_services_exist_under_a_fixed_project_name() -> None:
    assert COMPOSE["name"] == "waddle"
    assert set(SERVICES) == {"waddle-api", "ollama", "ollama-pull"}


def test_the_file_has_no_sections_or_service_settings_beyond_the_reviewed_ones() -> None:
    # An allowlist: a new key (include, configs, extends, env_file, group_add, sysctls,
    # labels, ...) must be added here on purpose, next to a test that checks it.
    assert set(COMPOSE) == {"name", "services", "networks", "volumes", "secrets"}
    common = {"image", "cap_drop", "security_opt", "networks", "volumes", "restart"}
    ollama = common | {"environment"}
    allowed = {
        "waddle-api": common
        | {"build", "user", "read_only", "tmpfs", "depends_on", "secrets", "healthcheck"},
        "ollama": ollama | {"deploy", "healthcheck"},
        "ollama-pull": ollama | {"profiles", "entrypoint"},
    }
    for name, service in SERVICES.items():
        assert set(service) <= allowed[name], name
    assert SERVICES["waddle-api"]["depends_on"] == ["ollama"]


def test_the_model_volume_and_the_egress_network_are_plain_docker_defaults() -> None:
    # A volume with driver options can be a host folder in disguise, and an external
    # network could be the host's own; both must stay Docker-managed and project-scoped.
    assert COMPOSE["volumes"] == {"ollama-models": {}}
    assert COMPOSE["networks"]["egress"] in ({}, None)


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_no_service_is_published_privileged_or_near_the_docker_socket(name: str) -> None:
    service = SERVICES[name]
    forbidden = {"ports", "network_mode", "privileged", "cap_add", "devices", "pid", "ipc"}
    forbidden |= {"extra_hosts", "volumes_from", "userns_mode", "cgroup_parent", "expose"}
    assert not forbidden & set(service)
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == NO_NEW_PRIVILEGES
    assert "docker.sock" not in yaml.safe_dump(service)
    assert "/var/run" not in yaml.safe_dump(service)


def test_the_models_network_is_internal_and_only_the_pull_service_has_a_route_out() -> None:
    assert COMPOSE["networks"]["models"] == {"internal": True}
    assert set(COMPOSE["networks"]) == {"models", "egress"}
    assert not (COMPOSE["networks"]["egress"] or {}).get("internal", False)
    default_profile = {n: s for n, s in SERVICES.items() if "profiles" not in s}
    assert set(default_profile) == {"waddle-api", "ollama"}
    for service in default_profile.values():
        assert networks_of(service) == ["models"]
    assert SERVICES["ollama-pull"]["profiles"] == ["pull"]
    assert networks_of(SERVICES["ollama-pull"]) == ["egress"]
    assert [n for n, s in SERVICES.items() if "egress" in networks_of(s)] == ["ollama-pull"]


def test_waddle_api_is_locked_down_and_runs_as_the_invoking_user() -> None:
    api = SERVICES["waddle-api"]
    assert re.fullmatch(r"\$\{WADDLE_UID(:\?[^}]*)?\}:\$\{WADDLE_GID(:\?[^}]*)?\}", api["user"])
    assert api["read_only"] is True
    assert [t.split(":")[0] for t in api["tmpfs"]] == ["/tmp"]
    assert api["build"] == {"context": "..", "dockerfile": "deploy/Dockerfile.api"}
    assert "deploy" not in api, "waddle-api must not reserve a GPU"
    assert "runtime" not in api and "gpus" not in api
    assert "environment" not in api and "env_file" not in api


def test_waddle_api_mounts_exactly_the_runtime_dir_the_config_and_the_token() -> None:
    api = SERVICES["waddle-api"]
    mounts = {m["target"]: m for m in api["volumes"]}
    assert set(mounts) == {"/run/waddle", "/etc/waddle/brain.toml"}
    runtime, config = mounts["/run/waddle"], mounts["/etc/waddle/brain.toml"]
    assert runtime["type"] == "bind"
    assert re.fullmatch(r"\$\{WADDLE_RUNTIME_DIR(:\?[^}]*)?\}", runtime["source"])
    assert runtime["bind"] == {"create_host_path": False}  # docker must not create it as root
    assert config == {
        "type": "bind",
        "source": "./brain.toml",
        "target": "/etc/waddle/brain.toml",
        "read_only": True,
    }
    assert api["secrets"] == ["waddle-link-token"]  # short form: read-only under /run/secrets
    secret = COMPOSE["secrets"]["waddle-link-token"]
    assert set(secret) == {"file"}
    assert re.fullmatch(r"\$\{WADDLE_TOKEN_FILE(:\?[^}]*)?\}", secret["file"])
    assert set(COMPOSE["secrets"]) == {"waddle-link-token"}


def test_the_container_paths_match_the_shipped_brain_config() -> None:
    config = tomllib.loads((DEPLOY / "brain.toml").read_text(encoding="utf-8"))
    assert config["socket_path"] == "/run/waddle/brain.sock"
    assert config["token_path"] == "/run/secrets/waddle-link-token"
    assert config["ollama_url"] == "http://ollama:11434"  # the service name on `models`


def test_ollama_is_pinned_quiet_and_the_only_service_with_a_gpu() -> None:
    ollama = SERVICES["ollama"]
    assert PINNED_OLLAMA.fullmatch(ollama["image"])
    devices = ollama["deploy"]["resources"]["reservations"]["devices"]
    assert devices == [{"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]
    assert [n for n, s in SERVICES.items() if "deploy" in s or "gpus" in s] == ["ollama"]
    assert ollama["volumes"] == ["ollama-models:/root/.ollama"]
    assert set(COMPOSE["volumes"]) == {"ollama-models"}
    for name in ("ollama", "ollama-pull"):
        env = SERVICES[name]["environment"]
        assert QUIET_OLLAMA.items() <= env.items(), name
    assert "build" not in ollama  # upstream image, unmodified


def test_long_running_services_have_healthchecks_and_restart() -> None:
    for name in ("waddle-api", "ollama"):
        assert SERVICES[name]["restart"] == "unless-stopped"
        assert SERVICES[name]["healthcheck"]["test"][0] == "CMD"
    assert "brain.sock" in SERVICES["waddle-api"]["healthcheck"]["test"][-1]
    assert SERVICES["ollama-pull"]["restart"] == "no"


def test_the_pull_service_shares_the_image_and_volume_and_takes_one_argument() -> None:
    pull, ollama = SERVICES["ollama-pull"], SERVICES["ollama"]
    assert pull["image"] == ollama["image"]
    assert pull["volumes"] == ollama["volumes"]
    assert pull["environment"]["OLLAMA_HOST"].startswith("127.0.0.1:")
    # Neither server may prune: a server that starts while a download is under way would
    # delete the layers that no finished model refers to yet.
    assert pull["environment"]["OLLAMA_NOPRUNE"] == "1"
    assert ollama["environment"]["OLLAMA_NOPRUNE"] == "1"
    assert "command" not in pull  # the model name is the only argument, given at run time
    assert pull["entrypoint"][:2] == ["/bin/sh", "-c"]
    assert len(pull["entrypoint"]) == 4


def run_pull_script(tmp_path: Path, list_exit: int, model: str) -> tuple[int, list[str]]:
    """Run the inline pull command with doubles for ``ollama`` and ``sleep`` on PATH."""
    calls = tmp_path / "calls"
    calls.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ollama").write_text(
        # One write per call: the background `serve` and the first `list` run at the same
        # time, and two writes each would interleave in the record.
        f'#!/bin/sh\nprintf "%s\\n" "$(printf "%s|" "$@")" >> {calls}\n'
        f'[ "$1" = list ] && exit {list_exit}\nexit 0\n'
    )
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    _sh, _c, script, arg0 = SERVICES["ollama-pull"]["entrypoint"]
    done = subprocess.run(
        ["/bin/sh", "-c", script.replace("$$", "$"), arg0, model],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=30,
        check=False,
    )
    # The background `serve` double may still be starting when the script has finished.
    deadline = time.monotonic() + 10
    while "serve|" not in calls.read_text().splitlines() and time.monotonic() < deadline:
        time.sleep(0.01)
    return done.returncode, calls.read_text().splitlines()


def test_the_pull_command_starts_a_server_waits_for_it_and_pulls_the_one_model(
    tmp_path: Path,
) -> None:
    code, calls = run_pull_script(tmp_path, list_exit=0, model="some model; echo hacked")
    assert code == 0
    assert "serve|" in calls
    pulls = [c for c in calls if c.startswith("pull")]
    assert pulls == ["pull|some model; echo hacked|"]  # one quoted argument, never shell
    assert calls.index("list|") < calls.index(pulls[0])
    assert set(calls) == {"serve|", "list|", pulls[0]}


def test_the_pull_command_gives_up_when_its_server_never_answers(tmp_path: Path) -> None:
    code, calls = run_pull_script(tmp_path, list_exit=1, model="m")
    assert code == 1
    assert calls.count("list|") == 30
    assert not [c for c in calls if c.startswith("pull")]


@pytest.mark.parametrize("profile", [[], ["--profile", "pull"]])
def test_docker_compose_accepts_the_file(tmp_path: Path, profile: list[str]) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is not installed")
    token = tmp_path / "token"
    token.touch()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "WADDLE_UID": "1000",
        "WADDLE_GID": "1000",
        "WADDLE_RUNTIME_DIR": str(tmp_path),
        "WADDLE_TOKEN_FILE": str(token),
    }
    head = [docker, "compose", "-p", "waddle", "-f", str(DEPLOY / "docker-compose.yml")]
    done = subprocess.run(
        [*head, "--env-file", os.devnull, *profile, "config"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if "is not a docker command" in done.stderr or "unknown shorthand flag" in done.stderr:
        pytest.skip("docker compose is not installed")
    assert done.returncode == 0, done.stderr
    rendered = yaml.safe_load(done.stdout)
    expected = {"waddle-api", "ollama"} | ({"ollama-pull"} if profile else set())
    assert set(rendered["services"]) == expected
    assert rendered["services"]["waddle-api"]["user"] == "1000:1000"
    assert rendered["networks"]["models"]["internal"] is True
    assert "ports" not in done.stdout


# --- Dockerfile.api and its ignore file ----------------------------------------------------


def dockerfile_instructions() -> list[tuple[str, str]]:
    joined = re.sub(r"\\\n", " ", DOCKERFILE)
    lines = [ln.strip() for ln in joined.splitlines() if ln.strip() and not ln.startswith("#")]
    return [(ln.split(None, 1)[0].upper(), ln.split(None, 1)[1]) for ln in lines]


def test_the_dockerfile_uses_a_pinned_python_and_never_copies_the_desktop() -> None:
    steps = dockerfile_instructions()
    bases = [arg.split()[0] for op, arg in steps if op == "FROM"]
    assert bases and set(bases) == {"python:3.13-slim"}
    copied = " ".join(arg for op, arg in steps if op in ("COPY", "ADD"))
    assert "packages/core" in copied and "packages/brain" in copied
    instructions = " ".join(arg for _, arg in steps).lower()
    assert "desktop" not in instructions and "qt" not in instructions
    assert not re.search(r"^\s*COPY\s+(--\S+\s+)*\.\s", DOCKERFILE, re.MULTILINE), "no COPY ."
    assert "latest" not in copied


def test_the_dockerfile_drops_root_exposes_nothing_and_starts_the_brain() -> None:
    steps = dockerfile_instructions()
    ops = [op for op, _ in steps]
    assert "EXPOSE" not in ops
    users = [arg for op, arg in steps if op == "USER"]
    assert users, "the image must not run as root"
    assert users[-1].split(":")[0] not in {"0", "root"}
    last_from = max(i for i, op in enumerate(ops) if op == "FROM")
    assert ops.index("USER") > last_from, "USER must be set in the final stage"
    assert steps[-1] == ("ENTRYPOINT", '["waddle-brain", "--config", "/etc/waddle/brain.toml"]')
    assert "--package waddle-brain" in DOCKERFILE and "--frozen" in DOCKERFILE


def test_the_ignore_file_sits_where_docker_reads_it_and_keeps_the_context_small() -> None:
    # With the repository root as context, docker reads <Dockerfile>.dockerignore beside the
    # Dockerfile; a plain deploy/.dockerignore would be silently unused.
    lines = (DEPLOY / "Dockerfile.api.dockerignore").read_text(encoding="utf-8").splitlines()
    rules = [ln for ln in lines if ln and not ln.startswith("#")]
    assert rules[0] == "*", "everything is excluded unless listed"
    allowed = {r[1:] for r in rules if r.startswith("!")}
    assert allowed == {"pyproject.toml", "uv.lock", "packages/core", "packages/brain"}
    excluded = set(rules) - {"*"} - {f"!{a}" for a in allowed}
    assert {"**/tests", "**/.git", "**/.venv", "**/target", "**/__pycache__"} <= excluded
    assert rules.index("**/tests") > max(rules.index(f"!{a}") for a in allowed)


# --- native config -------------------------------------------------------------------------


def test_the_native_config_has_the_same_roles_and_a_loopback_model_server() -> None:
    shipped = tomllib.loads((DEPLOY / "brain.toml").read_text(encoding="utf-8"))
    native = tomllib.loads((DEPLOY / "brain.native.toml").read_text(encoding="utf-8"))
    assert native["roles"] == shipped["roles"]
    assert native["ollama_url"] == "http://127.0.0.1:11434"
    assert native["socket_path"] == "@WADDLE_BRAIN_SOCKET@"
    assert native["token_path"] == "@WADDLE_TOKEN_FILE@"
    assert set(native) == set(shipped)
