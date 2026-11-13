"""The only place the host starts docker, or the native brain (ARCHITECTURE §1 I2, §5.1).

A closed list of operations on Waddle's OWN compose project, each a fixed argv:

- ``up``      ``docker compose ... up --detach --build``
- ``down``    ``docker compose ... down`` (containers and networks; model volume is kept)
- ``status``  ``docker compose ... ps --all --format json``
- ``logs``    ``docker compose ... logs --no-color --tail N`` (for doctor; never logged)
- ``pull``    ``docker compose ... --profile pull run --rm -T ollama-pull <model>``

where ``...`` is always ``-p waddle -f <the compose file shipped with Waddle> --env-file
/dev/null``. No caller can name another project, file, service or docker subcommand. The
model to pull is looked up by role in the shipped brain config; it is never free text.

Native mode starts ``waddle-brain`` from the same Python environment as a child process
with a generated config in Waddle's runtime directory, and stops only that child.

There is no shell, the environment is built here (so a stray ``COMPOSE_FILE`` or
``COMPOSE_PROFILES`` cannot widen anything), and every call has a timeout. Problems come
back as a ``Result`` with a plain-English reason; nothing here raises to the caller. Log
lines carry the operation, the outcome, the exit status and the time taken, nothing else.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, get_args

from waddle_core.link import Role
from waddle_core.paths import WaddlePaths
from waddle_core.untrusted import clean
from waddle_desktop import log

PROJECT: Final = "waddle"
SERVICES: Final = ("waddle-api", "ollama")
PULL_SERVICE: Final = "ollama-pull"
PULL_PROFILE: Final = "pull"

# The files shipped with Waddle, next to the source tree this module was loaded from.
DEPLOY_DIR: Final = Path(__file__).resolve().parents[5] / "deploy"
COMPOSE_FILE: Final = DEPLOY_DIR / "docker-compose.yml"
BRAIN_CONFIG: Final = DEPLOY_DIR / "brain.toml"
NATIVE_TEMPLATE: Final = DEPLOY_DIR / "brain.native.toml"
NATIVE_CONFIG_NAME: Final = "brain.native.toml"

# Looked up in these fixed places, never through the caller's PATH.
DOCKER_CANDIDATES: Final = ("/usr/bin/docker", "/usr/local/bin/docker", "/bin/docker")
# The same rule as RoleConfig.model in waddle-brain (a test keeps the two equal).
MODEL_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$"
MODEL_MAX_CHARS: Final = 128

UP_SECONDS: Final = 1800.0  # the first run downloads one image and builds another
DOWN_SECONDS: Final = 120.0
QUERY_SECONDS: Final = 15.0
PULL_SECONDS: Final = 6 * 3600.0  # models are tens of gigabytes
NATIVE_SETTLE_SECONDS: Final = 0.5  # a brain with a bad config or token exits within this
NATIVE_STOP_SECONDS: Final = 5.0
MAX_LOG_TAIL: Final = 1000
MAX_LOG_CHARS: Final = 64 * 1024

_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
# What docker itself needs to find the user's daemon and config. No COMPOSE_* on purpose.
_DOCKER_ENV: Final = ("HOME", "XDG_RUNTIME_DIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
_NATIVE_ENV: Final = ("HOME", "LANG")
_MODEL: Final = re.compile(MODEL_PATTERN)
_WORD: Final = re.compile(r"[a-z][a-z_-]{0,31}")
_LITERAL_PATH: Final = re.compile(r"/[^'\x00-\x1f\x7f]*")  # fits a TOML literal string
_ROLES: Final[tuple[str, ...]] = get_args(Role)


class Outcome(StrEnum):
    OK = "ok"
    FAILED = "failed"  # the command ran and reported an error, or timed out
    UNAVAILABLE = "unavailable"  # nothing could be run: docker, its daemon or a file is missing


@dataclass(frozen=True)
class Result:
    """How an operation ended. ``reason`` is a plain sentence for the user; empty when ok."""

    outcome: Outcome
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


@dataclass(frozen=True)
class ServiceStatus:
    service: str  # one of SERVICES
    state: str  # docker's word: "running", "exited", "restarting", ... or "unknown"
    health: str  # "healthy", "unhealthy", "starting", or "" when docker reports none


@dataclass(frozen=True)
class Status(Result):
    services: tuple[ServiceStatus, ...] = ()  # services with no container are absent


@dataclass(frozen=True)
class Logs(Result):
    text: str = ""  # untrusted container output, cleaned and capped. Never log it.


class ComposeError(ValueError):
    """An argument is outside what this module will pass to docker."""


# --- argv builders (pure) ------------------------------------------------------------------


def _compose(*tail: str, profile: str | None = None) -> list[str]:
    head = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), "--env-file", os.devnull]
    return [*head, *(["--profile", profile] if profile else []), *tail]


def up_argv() -> list[str]:
    return _compose("up", "--detach", "--build")


def down_argv() -> list[str]:
    return _compose("down")


def ps_argv() -> list[str]:
    return _compose("ps", "--all", "--format", "json")


def logs_argv(tail: int) -> list[str]:
    if isinstance(tail, bool) or not isinstance(tail, int) or not 1 <= tail <= MAX_LOG_TAIL:
        raise ComposeError("tail is out of range")
    return _compose("logs", "--no-color", "--tail", str(tail))


def pull_argv(model: str) -> list[str]:
    """The one-shot pull of ``model``. Refuses anything that is not a plain model name."""
    return _compose("run", "--rm", "-T", PULL_SERVICE, checked_model(model), profile=PULL_PROFILE)


def checked_model(model: object) -> str:
    if not isinstance(model, str) or len(model) > MODEL_MAX_CHARS or not _MODEL.fullmatch(model):
        raise ComposeError("not a model name")
    return model


def compose_env(paths: WaddlePaths, environ: Mapping[str, str]) -> dict[str, str]:
    """The whole environment docker gets: its own lookups plus the four compose variables."""
    env = {name: environ[name] for name in _DOCKER_ENV if name in environ}
    env["PATH"] = _PATH
    env["WADDLE_UID"] = str(os.getuid())
    env["WADDLE_GID"] = str(os.getgid())
    env["WADDLE_RUNTIME_DIR"] = str(paths.runtime_dir)
    env["WADDLE_TOKEN_FILE"] = str(paths.link_token)
    return env


def native_argv(config: Path) -> list[str]:
    return ["waddle-brain", "--config", str(config)]


# --- parsing (pure) ------------------------------------------------------------------------


def configured_models(config_file: Path = BRAIN_CONFIG) -> dict[Role, str]:
    """Role -> model name from the brain config. Raises ``ComposeError`` if it is not usable."""
    try:
        roles = tomllib.loads(config_file.read_text(encoding="utf-8"))["roles"]
        return {
            role: checked_model(roles[role]["model"]) for role in get_args(Role) if role in roles
        }
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        raise ComposeError("brain config is missing or not valid") from None


def parse_ps(text: str) -> tuple[ServiceStatus, ...]:
    """Read ``ps --format json``: one JSON object per line, or one array in older versions."""
    rows: list[object] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        rows += item if isinstance(item, list) else [item]
    found: dict[str, ServiceStatus] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("Service") in SERVICES:
            service = str(row["Service"])
            found[service] = ServiceStatus(
                service, _word(row.get("State")) or "unknown", _word(row.get("Health"))
            )
    return tuple(found[name] for name in SERVICES if name in found)


def _word(value: object) -> str:
    """Docker's state words are lower-case identifiers; anything else is dropped."""
    return value if isinstance(value, str) and _WORD.fullmatch(value) else ""


def render_native_config(template: str, paths: WaddlePaths) -> str:
    """Fill the two placeholders of ``brain.native.toml`` with this user's real paths."""
    socket, token = str(paths.brain_socket), str(paths.link_token)
    if not (_LITERAL_PATH.fullmatch(socket) and _LITERAL_PATH.fullmatch(token)):
        raise ComposeError("path cannot be written to the config")
    text = template.replace("@WADDLE_BRAIN_SOCKET@", socket).replace("@WADDLE_TOKEN_FILE@", token)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ComposeError("native config template is not valid") from None
    if data.get("socket_path") != socket or data.get("token_path") != token:
        raise ComposeError("native config template has lost its placeholders")
    return text


# UNVERIFIED (spike S3.1): the daemon-down text was seen with Docker 29 / Compose 5; the
# other needles come from documentation and older versions, not from a reproduced failure.
_STDERR_REASONS: Final = (
    (
        ("failed to connect to the docker api", "cannot connect to the docker daemon"),
        Outcome.UNAVAILABLE,
        "Docker is installed but its service is not running.",
    ),
    (
        ("permission denied while trying to connect",),
        Outcome.UNAVAILABLE,
        "Your user is not allowed to use Docker.",
    ),
    (
        ("is not a docker command", "unknown shorthand flag: 'p'"),
        Outcome.UNAVAILABLE,
        "The Docker Compose plugin is not installed.",
    ),
    (
        ("could not select device driver",),
        Outcome.FAILED,
        "Docker could not give the model server a GPU. "
        "The NVIDIA Container Toolkit may be missing.",
    ),
)


def classify_failure(stderr: str) -> Result:
    """Turn docker's error text into one of a few fixed sentences. The text is not kept."""
    lowered = stderr.lower()
    for needles, outcome, reason in _STDERR_REASONS:
        if any(needle in lowered for needle in needles):
            return Result(outcome, reason)
    return Result(Outcome.FAILED, "Docker reported an error.")


# --- the controller ------------------------------------------------------------------------


class BrainCtl:
    """Runs the operations above for one user. Blocking: call it off the UI thread.

    ``docker`` and ``brain_program`` replace the fixed lookups with an argv prefix; tests use
    them to substitute a double.
    """

    def __init__(
        self,
        paths: WaddlePaths,
        *,
        docker: Sequence[str] | None = None,
        brain_program: Sequence[str] | None = None,
    ) -> None:
        self._paths = paths
        self._docker = docker
        self._brain_program = brain_program
        self._native: subprocess.Popen[bytes] | None = None

    def up(self) -> Result:
        """Build and start the containers. The link token must already exist."""
        if not _is_plain_file(self._paths.link_token):
            return Result(Outcome.UNAVAILABLE, "The link token has not been created yet.")
        if not _is_plain_file(BRAIN_CONFIG):  # mounted into waddle-api; docker must find it
            return Result(Outcome.UNAVAILABLE, "Waddle's brain config is missing.")
        if not self._make_runtime_dir():
            return Result(Outcome.UNAVAILABLE, "Waddle's runtime folder could not be created.")
        return self._run("up", up_argv(), UP_SECONDS)[0]

    def down(self) -> Result:
        return self._run("down", down_argv(), DOWN_SECONDS)[0]

    def status(self) -> Status:
        result, out = self._run("status", ps_argv(), QUERY_SECONDS)
        return Status(result.outcome, result.reason, parse_ps(out) if result.ok else ())

    def logs(self, tail: int = 200) -> Logs:
        """The last lines the containers printed, for doctor to show. Untrusted text."""
        argv = logs_argv(max(1, min(int(tail), MAX_LOG_TAIL)))
        result, out = self._run("logs", argv, QUERY_SECONDS, merge_stderr=True)
        text = clean(out[-MAX_LOG_CHARS:], MAX_LOG_CHARS) if result.ok else ""
        return Logs(result.outcome, result.reason, text)

    def pull_model(self, role: Role, *, progress: bool = False) -> Result:
        """Download the model the shipped brain config names for ``role``.

        With ``progress`` the download's own output goes straight to this process's
        terminal (for the CLI) instead of being discarded.
        """
        try:
            model = configured_models(BRAIN_CONFIG).get(role) if role in _ROLES else None
            if model is None:
                return Result(Outcome.FAILED, "The brain config names no model for that role.")
            argv = pull_argv(model)
        except ComposeError:
            return Result(Outcome.FAILED, "The brain config is missing or has a bad model name.")
        return self._run("pull", argv, PULL_SECONDS, capture=not progress)[0]

    def start_native(self) -> Result:
        """Start ``waddle-brain`` as a child of this process, outside any container."""
        if self.native_running():
            return Result(Outcome.OK)
        program = self._brain_program or _installed_brain()
        if not program:
            return Result(Outcome.UNAVAILABLE, "The brain is not installed next to Waddle.")
        try:
            text = render_native_config(NATIVE_TEMPLATE.read_text(encoding="utf-8"), self._paths)
        except (OSError, UnicodeDecodeError, ComposeError):
            return Result(Outcome.UNAVAILABLE, "Waddle's native brain config could not be made.")
        config = self._paths.runtime_dir / NATIVE_CONFIG_NAME
        if not self._make_runtime_dir() or not _write_private(config, text):
            return Result(Outcome.UNAVAILABLE, "Waddle's runtime folder could not be written.")
        env = {name: os.environ[name] for name in _NATIVE_ENV if name in os.environ}
        env["PATH"] = _PATH
        started = time.monotonic()
        try:
            child = subprocess.Popen(  # noqa: S603 - fixed argv: Waddle's own brain
                [*program, *native_argv(config)[1:]],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=None,  # the brain's own log is already an allowlist
                env=env,
            )
        except OSError:
            config.unlink(missing_ok=True)
            log.warning("brainctl", op="native_start", outcome=Outcome.UNAVAILABLE)
            return Result(Outcome.UNAVAILABLE, "The brain could not be started.")
        try:
            status = child.wait(timeout=NATIVE_SETTLE_SECONDS)
        except subprocess.TimeoutExpired:
            self._native = child
            log.event("brainctl", op="native_start", outcome=Outcome.OK, ms=_ms(started))
            return Result(Outcome.OK)
        config.unlink(missing_ok=True)
        log.warning("brainctl", op="native_start", outcome=Outcome.FAILED, exit=status)
        return Result(Outcome.FAILED, f"The brain stopped right away (exit status {status}).")

    def stop_native(self) -> Result:
        """Stop the child started by ``start_native``. Touches no other process."""
        child, self._native = self._native, None
        if child is None:
            return Result(Outcome.OK)
        started = time.monotonic()
        child.terminate()
        try:
            child.wait(timeout=NATIVE_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        (self._paths.runtime_dir / NATIVE_CONFIG_NAME).unlink(missing_ok=True)
        log.event("brainctl", op="native_stop", outcome=Outcome.OK, ms=_ms(started))
        return Result(Outcome.OK)

    def native_running(self) -> bool:
        return self._native is not None and self._native.poll() is None

    def _make_runtime_dir(self) -> bool:
        try:
            self._paths.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            return False
        return True

    def _run(
        self,
        op: str,
        argv: Sequence[str],
        timeout: float,
        *,
        capture: bool = True,
        merge_stderr: bool = False,
    ) -> tuple[Result, str]:
        """Run one built argv through docker; return how it went and its standard output."""
        result, out, status = self._execute(argv, timeout, capture, merge_stderr)
        log.event("brainctl", op=op, outcome=result.outcome, exit=status)
        return result, out

    def _execute(
        self, argv: Sequence[str], timeout: float, capture: bool, merge_stderr: bool
    ) -> tuple[Result, str, int | None]:
        if not _is_plain_file(COMPOSE_FILE):
            reason = "Waddle's compose file is missing. Run Waddle from its source folder."
            return Result(Outcome.UNAVAILABLE, reason), "", None
        prefix = self._docker or _installed_docker()
        if not prefix:
            return Result(Outcome.UNAVAILABLE, "Docker is not installed."), "", None
        try:
            done = subprocess.run(  # noqa: S603 - argv comes only from the builders above
                [*prefix, *argv[1:]],
                shell=False,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else None,
                stderr=(subprocess.STDOUT if merge_stderr else subprocess.PIPE)
                if capture
                else None,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=compose_env(self._paths, os.environ),
            )
        except subprocess.TimeoutExpired:
            return Result(Outcome.FAILED, "Docker did not finish in time."), "", None
        except OSError:
            return Result(Outcome.UNAVAILABLE, "Docker could not be started."), "", None
        out = done.stdout or ""
        if done.returncode == 0:
            return Result(Outcome.OK), out, 0
        return classify_failure(out if merge_stderr else done.stderr or ""), "", done.returncode


def _installed_docker() -> list[str]:
    return next(([path] for path in DOCKER_CANDIDATES if os.access(path, os.X_OK)), [])


def _installed_brain() -> list[str]:
    """``waddle-brain`` from the environment this process runs in, and from nowhere else."""
    script = Path(sys.executable).parent / "waddle-brain"
    return [str(script)] if os.access(script, os.X_OK) else []


def _is_plain_file(path: Path) -> bool:
    """A regular file that is not reached through a symlink at its last step."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _write_private(path: Path, text: str) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        return False
    return True


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
