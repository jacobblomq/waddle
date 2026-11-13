"""``waddle doctor``: what works on this machine, what does not, and what to do about it.

Read-only. It asks the session a few questions, starts Waddle's own capture helper to say
hello, and prints the answers. It captures nothing and changes nothing.

The brain checks go through ``BrainCtl`` (one ``status`` call), a few file reads, and one
handshake on the brain's private socket. A check that could not be made is reported as
"not checked", never as fine.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from waddle_core.config import ConfigError, WaddleConfig, load_config
from waddle_core.identity import CAPTURE_APP_ID
from waddle_core.paths import WaddlePaths
from waddle_core.privacy import BlocklistError, load_blocklist
from waddle_core.untrusted import clean
from waddle_desktop.brainctl import compose
from waddle_desktop.cli import models_cmd
from waddle_desktop.resources import data_base, default_blocklist_path

# SKIP: the check could not be made. NOTE: a plain fact that needs no action.
OK, WARN, BAD, SKIP, NOTE = "ok", "warn", "fail", "skip", "note"
_MARK = {OK: "✓", WARN: "!", BAD: "✗", SKIP: "?", NOTE: "-"}

NVIDIA_VERSION_FILE: Final = Path("/proc/driver/nvidia/version")
MIN_BWRAP: Final = "0.8"


@dataclass(frozen=True)
class Check:
    area: str
    level: str
    text: str
    fix: str = ""


def check_session(env: Mapping[str, str]) -> list[Check]:
    kind = env.get("XDG_SESSION_TYPE", "")
    desktop = env.get("XDG_CURRENT_DESKTOP", "")
    out = [
        Check("session", OK, "Wayland session")
        if kind == "wayland"
        else Check(
            "session", BAD, f"session type is '{kind or 'unknown'}'", "Log in to Plasma (Wayland)."
        ),
        Check("session", OK, "KDE Plasma")
        if "KDE" in desktop.upper()
        else Check(
            "session",
            WARN,
            f"desktop is '{desktop or 'unknown'}', not KDE",
            "Waddle is built for Plasma 6.",
        ),
    ]
    return out


def check_files(paths: WaddlePaths, data_home: Path, base: Path) -> list[Check]:
    out: list[Check] = []
    for script in ("waddle-core.js", "waddle-cursor.js"):
        present = (base / "kwin" / script).is_file()
        out.append(
            Check(
                "kwin",
                OK if present else BAD,
                f"KWin script {script} {'found' if present else 'missing'}",
                "" if present else "Reinstall Waddle.",
            )
        )
    try:
        load_blocklist(default_blocklist_path(base))
        out.append(Check("privacy", OK, "default private list loads"))
    except (OSError, BlocklistError):
        out.append(
            Check("privacy", BAD, "default private list cannot be read", "Reinstall Waddle.")
        )

    helper = paths.capture_bin
    entry = data_home / "applications" / f"{CAPTURE_APP_ID}.desktop"
    if not helper.is_file():
        out.append(
            Check(
                "capture",
                BAD,
                "capture helper is not installed",
                "Install Rust (cargo), then run ./install.sh again.",
            )
        )
    else:
        out.append(Check("capture", OK, "capture helper installed"))
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if (
            f"Exec={helper}\n" in text
            and "X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2" in text
        ):
            out.append(Check("capture", OK, "helper's desktop entry names the installed binary"))
        else:
            out.append(
                Check(
                    "capture",
                    BAD,
                    "helper's desktop entry is missing or points elsewhere",
                    "Run ./install.sh again; KWin authorizes the helper by this file.",
                )
            )
    for tool in ("kwriteconfig6", "kreadconfig6"):
        if shutil.which(tool) is None:
            out.append(
                Check("kwin", WARN, f"{tool} not found", "Needed for the stay-on-top window rule.")
            )
    return out


def check_config(paths: WaddlePaths) -> tuple[WaddleConfig | None, list[Check]]:
    try:
        config = load_config(paths.config_file)
    except ConfigError:
        return None, [
            Check(
                "config",
                BAD,
                "config.toml is not valid",
                f"Fix or remove {paths.config_file}. Waddle captures nothing until then.",
            )
        ]
    return config, [
        Check(
            "config",
            OK,
            "config.toml is valid"
            if paths.config_file.exists()
            else "no config.toml (defaults in use)",
        )
    ]


async def check_helper(paths: WaddlePaths) -> list[Check]:
    from waddle_desktop.sensors.capture_client import CaptureClientError
    from waddle_desktop.sensors.helper_proc import HelperUnavailableError
    from waddle_desktop.sensors.senses import open_helper_eye

    try:
        eye = await open_helper_eye(paths.capture_bin)
    except HelperUnavailableError as exc:
        return (
            []
            if str(exc) == "missing"
            else [
                Check(
                    "capture",
                    BAD,
                    f"capture helper cannot be started ({exc})",
                    "Run ./install.sh again.",
                )
            ]
        )
    try:
        hello = await eye.hello()
    except CaptureClientError as exc:
        return [
            Check(
                "capture",
                BAD,
                f"capture helper did not answer ({type(exc).__name__})",
                "Run ./install.sh again.",
            )
        ]
    finally:
        eye.close()
    out = [Check("capture", OK, f"capture helper {hello.version} answers")]
    if not hello.kwin or hello.screenshot2_version is None:
        out.append(
            Check(
                "capture",
                BAD,
                "KWin's ScreenShot2 service is not reachable",
                "Are you in a Plasma Wayland session?",
            )
        )
    else:
        regime = {
            "restricted": "this Plasma authorizes the helper by its desktop entry",
            "open": "this Plasma needs no authorization",
            "unknown": "could not tell how this Plasma authorizes captures",
        }[hello.permission_regime]
        out.append(
            Check(
                "capture",
                OK if hello.permission_regime != "unknown" else WARN,
                f"KWin ScreenShot2 v{hello.screenshot2_version}; {regime}",
            )
        )
    out.append(
        Check("privacy", OK, "helper can read the lock state")
        if hello.lock_check
        else Check(
            "privacy",
            BAD,
            "helper cannot read the lock state (logind)",
            "Waddle captures nothing while this is so.",
        )
    )
    if not hello.config_ok:
        out.append(
            Check(
                "privacy",
                BAD,
                "helper cannot read config.toml",
                "Waddle captures nothing until it is fixed.",
            )
        )
    return out


async def check_live(config: WaddleConfig) -> list[Check]:
    from waddle_desktop.sensors.hotkey_setup import make_plan
    from waddle_desktop.sensors.session import SessionWatcher

    out: list[Check] = []
    watcher = SessionWatcher(on_change=lambda _s: None)
    try:
        snapshot = await asyncio.wait_for(watcher.start(), 6.0)
    except Exception:
        out.append(
            Check(
                "session", BAD, "session services did not answer", "Is the session D-Bus running?"
            )
        )
    else:
        out.append(
            Check("session", OK, "lock state is readable")
            if snapshot.lock_known
            else Check(
                "session",
                BAD,
                "lock state is not readable",
                "Waddle keeps his eyes closed while this is so.",
            )
        )
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(watcher.stop(), 2.0)

    try:
        plan = await asyncio.wait_for(make_plan(config.hotkeys), 8.0)
    except Exception:
        return [*out, Check("hotkeys", WARN, "could not check hotkeys")]
    if not plan.conflicts_checked:
        out.append(
            Check(
                "hotkeys",
                WARN,
                "existing shortcuts could not be read",
                "Waddle will ask the system dialog to let you pick keys.",
            )
        )
    for action, _why in plan.invalid.items():
        out.append(
            Check(
                "hotkeys",
                BAD,
                f"{action}: the configured key is not usable",
                "Change it in config.toml [hotkeys].",
            )
        )
    for item in plan.plans:
        if not plan.conflicts_checked:
            out.append(Check("hotkeys", WARN, f"{item.action}: {item.wanted} not checked"))
        elif item.conflict is None:
            out.append(Check("hotkeys", OK, f"{item.action}: {item.wanted} is free"))
        else:
            holder = item.conflict.component_friendly or "another shortcut"
            options = ", ".join(str(a) for a in item.alternatives) or "none found"
            out.append(
                Check(
                    "hotkeys",
                    WARN,
                    f"{item.action}: {item.wanted} is taken by {holder}",
                    f"Free alternatives: {options}. Set one in config.toml [hotkeys].",
                )
            )
    return out


# ---- Phase 3: Docker, GPU, the brain link, models, the sandbox, app packs ------------------


class StatusSource(Protocol):
    """The one ``BrainCtl`` operation doctor uses."""

    def status(self) -> compose.Status: ...


def docker_installed() -> bool:
    """Is there a docker program where ``BrainCtl`` looks for it? A file check only."""
    return any(os.access(path, os.X_OK) for path in compose.DOCKER_CANDIDATES)


# BrainCtl's own sentences for the failures it can tell apart, asked for rather than copied.
def _reason_for(stderr: str) -> str:
    return compose.classify_failure(stderr).reason


_DAEMON_DOWN: Final = _reason_for("cannot connect to the docker daemon")
_NOT_ALLOWED: Final = _reason_for("permission denied while trying to connect")
_NO_COMPOSE: Final = _reason_for("is not a docker command")


def check_docker(ctl: StatusSource, installed: bool) -> list[Check]:
    """Docker CLI, daemon and Compose plugin, from one ``BrainCtl.status()`` call."""
    if not installed:
        return [
            Check(
                "docker",
                BAD,
                "Docker is not installed",
                "Install Docker Engine with its Compose plugin yourself (it needs root; "
                "Waddle does not do it). Or run the brain without it: waddle brain up --native",
            ),
            Check("docker", SKIP, "Docker service: not checked"),
            Check("docker", SKIP, "Docker Compose plugin: not checked"),
            Check("docker", SKIP, "brain containers: not checked"),
        ]
    out = [Check("docker", OK, "Docker is installed")]
    status = ctl.status()
    if status.ok:
        out.append(Check("docker", OK, "Docker service answers"))
        out.append(Check("docker", OK, "Docker Compose plugin works"))
        by_name = {service.service: service for service in status.services}
        for name in compose.SERVICES:
            service = by_name.get(name)
            if service is None:
                out.append(
                    Check("docker", WARN, f"{name}: not started", "Start it: waddle brain up")
                )
            elif service.state == "running" and service.health in ("", "healthy"):
                health = f" ({service.health})" if service.health else ""
                out.append(Check("docker", OK, f"{name}: running{health}"))
            else:
                health = f" ({service.health})" if service.health else ""
                out.append(
                    Check(
                        "docker",
                        WARN,
                        f"{name}: {service.state}{health}",
                        "See why: waddle brain logs",
                    )
                )
        return out
    if status.reason == _DAEMON_DOWN:
        out.append(
            Check(
                "docker",
                BAD,
                "Docker service is not running",
                "Start it yourself, for example: sudo systemctl start docker",
            )
        )
        out.append(Check("docker", SKIP, "Docker Compose plugin: not checked"))
    elif status.reason == _NOT_ALLOWED:
        out.append(
            Check(
                "docker",
                BAD,
                "your user is not allowed to use Docker",
                "Add yourself to the docker group (or set up rootless Docker), then log in again.",
            )
        )
        out.append(Check("docker", SKIP, "Docker Compose plugin: not checked"))
    elif status.reason == _NO_COMPOSE:
        out.append(Check("docker", SKIP, "Docker service: not checked"))
        out.append(
            Check(
                "docker",
                BAD,
                "Docker Compose plugin is not installed",
                "Install the docker-compose-plugin package yourself.",
            )
        )
    else:
        # BrainCtl's reason is one of its own fixed sentences, safe to show.
        out.append(Check("docker", BAD, status.reason or "Docker could not be asked"))
        out.append(Check("docker", SKIP, "Docker service: not checked"))
        out.append(Check("docker", SKIP, "Docker Compose plugin: not checked"))
    out.append(Check("docker", SKIP, "brain containers: not checked"))
    return out


_DRIVER_VERSION: Final = re.compile(r"(?<![\d.])(\d{2,4}\.\d{1,3}(?:\.\d{1,3})?)(?![\d.])")


def check_gpu(version_file: Path = NVIDIA_VERSION_FILE) -> list[Check]:
    """The host NVIDIA driver, from the kernel's own text file. Nothing is run."""
    # UNVERIFIED (spike S3.1): the file's layout ("NVRM version: ... Kernel Module  <version>")
    # is from NVIDIA's documentation; this container has no GPU to read it from.
    try:
        with version_file.open("rb") as handle:
            text = handle.read(4096).decode("ascii", errors="replace")
    except FileNotFoundError:
        driver = Check(
            "gpu",
            WARN,
            "NVIDIA driver is not loaded",
            "The model server expects an NVIDIA GPU. Install the driver yourself if you have one.",
        )
    except OSError:
        driver = Check("gpu", SKIP, "NVIDIA driver: not checked (its version file cannot be read)")
    else:
        found = _DRIVER_VERSION.search(text.partition("\n")[0])
        driver = Check(
            "gpu",
            OK,
            f"NVIDIA driver {found.group(1)} is loaded" if found else "NVIDIA driver is loaded",
        )
    return [
        driver,
        Check(
            "gpu",
            SKIP,
            "NVIDIA container runtime: not checked",
            "Waddle cannot ask Docker about it. If it is missing, waddle brain up says so; "
            "then install the NVIDIA Container Toolkit yourself.",
        ),
    ]


def check_token(paths: WaddlePaths) -> tuple[bool, list[Check]]:
    """Is the link token a private file of ours? The value is read to validate it, not shown."""
    from waddle_desktop.link.token import read_token

    try:
        info = os.lstat(paths.link_token)
    except FileNotFoundError:
        return False, [Check("brain", WARN, "no link token yet", "waddle brain up creates it.")]
    except OSError:
        return False, [Check("brain", SKIP, "link token: not checked (cannot be read)")]
    mode = stat.S_IMODE(info.st_mode)
    if read_token(paths.link_token) is None:
        return False, [
            Check(
                "brain",
                BAD,
                "link token is not usable (not a plain private file, or damaged)",
                f"Stop the brain, remove {paths.link_token} yourself, then: waddle brain up",
            )
        ]
    if mode != 0o600:
        return True, [
            Check(
                "brain",
                WARN,
                f"link token is private, but its mode is {mode:03o}, not 600",
                f"chmod 600 {paths.link_token}",
            )
        ]
    return True, [Check("brain", OK, "link token is present and private (mode 600)")]


async def check_brain(
    paths: WaddlePaths,
    probe: Callable[[WaddlePaths], Awaitable[models_cmd.Probe]] = models_cmd.probe_brain,
) -> tuple[models_cmd.Probe | None, list[Check]]:
    """The link token, the socket, and one handshake. Returns the probe when one was made."""
    from waddle_desktop.link.client import check_socket

    token_ok, out = check_token(paths)
    if (paths.runtime_dir / compose.NATIVE_CONFIG_NAME).exists():
        out.append(
            Check(
                "brain",
                WARN,
                "a native-mode brain config is present",
                "A brain run with --native has no network or file boundary (deploy/README.md).",
            )
        )
    unsafe = check_socket(paths.brain_socket)
    if unsafe == "no_socket":
        out.append(
            Check("brain", WARN, "no brain socket: the brain is not running", "waddle brain up")
        )
    elif unsafe is not None:
        out.append(
            Check(
                "brain",
                BAD,
                "brain socket is not safe to use",
                "It must be a socket you own, in a folder only you can open. "
                "Restart the brain: waddle brain down, then waddle brain up",
            )
        )
    else:
        out.append(Check("brain", OK, "brain socket is present and private"))
    if unsafe is not None or not token_ok:
        out.append(Check("brain", SKIP, "brain answers: not checked"))
        return None, out
    answer = await probe(paths)
    if answer.up:
        out.append(Check("brain", OK, "brain answers"))
    elif answer.reason == "busy":
        out.append(Check("brain", OK, "brain answers (it is in use by the running Waddle)"))
    elif answer.reason == "bad_token":
        out.append(
            Check(
                "brain",
                BAD,
                "brain refused Waddle's token",
                "Restart the brain: waddle brain down, then waddle brain up",
            )
        )
    else:
        out.append(
            Check("brain", BAD, f"brain did not answer ({answer.why()})", "See: waddle brain logs")
        )
    return answer, out


def check_models(
    probe: models_cmd.Probe | None, configured: models_cmd.Configured = compose.configured_models
) -> list[Check]:
    """Per role: downloaded and loaded, as the brain reported it; else "not checked"."""
    try:
        names = configured()
    except compose.ComposeError:
        return [
            Check(
                "models",
                BAD,
                "brain config (deploy/brain.toml) is missing or not valid",
                "Run Waddle from its source folder, or restore the file.",
            )
        ]
    answer = probe if probe is not None else models_cmd.Probe(False, "not_probed")
    out: list[Check] = []
    for role in models_cmd.ROLES:
        if role not in names:
            out.append(Check("models", WARN, f"{role}: no model configured"))
            continue
        line, present = models_cmd.model_line(role, names[role], answer)
        if present is None:
            out.append(Check("models", SKIP, line))
        elif present:
            out.append(Check("models", OK, line))
        else:
            out.append(Check("models", WARN, line, f"waddle models pull {role}"))
    return out


class _SandboxAnswer(Protocol):
    @property
    def available(self) -> bool: ...
    @property
    def reason(self) -> str: ...


async def _sandbox_status() -> _SandboxAnswer:
    from waddle_desktop.sandbox.readonly_exec import sandbox_status

    return await sandbox_status()


def _tool_present(name: str) -> bool:
    from waddle_desktop.sandbox.readonly_exec import TOOL_DIRS, find_tool

    return find_tool([f"{directory}/{name}" for directory in TOOL_DIRS]) is not None


async def check_sandbox(
    status: Callable[[], Awaitable[_SandboxAnswer]] = _sandbox_status,
    tool_present: Callable[[str], bool] = _tool_present,
) -> list[Check]:
    needs = f"Needs bubblewrap {MIN_BWRAP} or newer and util-linux (prlimit)."
    try:
        answer = await asyncio.wait_for(status(), 10.0)
    except Exception:
        out = [Check("sandbox", SKIP, "sandbox: not checked (the check itself failed)", needs)]
    else:
        if answer.available:
            out = [Check("sandbox", OK, "sandbox works (bubblewrap + prlimit)")]
        else:
            # The reason is one of readonly_exec's own fixed sentences.
            out = [
                Check(
                    "sandbox",
                    WARN,
                    f"sandbox is not available: {clean(answer.reason, 80)}",
                    f"{needs} Until then Waddle reads no man pages and asks no program for "
                    "its version.",
                )
            ]
    out.append(
        Check("sandbox", SKIP, f"bubblewrap version: not checked ({MIN_BWRAP} or newer is needed)")
    )
    for tool, package in (("man", "man-db"), ("tldr", "a tldr client")):
        found = tool_present(tool)
        out.append(
            Check(
                "sandbox",
                NOTE,
                f"{tool}: {'found' if found else 'not found'} (optional, from {package})",
            )
        )
    return out


def check_packs(directory: Path | None = None) -> list[Check]:
    """How many app packs load, and which files were skipped."""
    from waddle_desktop.identity.packs import default_packs_dir, load_packs

    index = load_packs(directory if directory is not None else default_packs_dir())
    count = len(index.packs)
    out = [
        Check("apps", OK, f"{count} app packs loaded")
        if count
        else Check("apps", BAD, "no app packs loaded", "Reinstall Waddle.")
    ]
    for error in index.errors:
        # Pack files are Waddle's own, but a damaged one may hold anything: cap and clean.
        name = clean(error.file, 64) or "pack folder"
        out.append(
            Check(
                "apps",
                WARN,
                f"{name}: skipped ({clean(error.reason, 120)})",
                "Reinstall Waddle to restore its app packs.",
            )
        )
    return out


async def collect_phase3(paths: WaddlePaths, ctl: StatusSource | None = None) -> list[Check]:
    if ctl is None:
        ctl = compose.BrainCtl(paths)
    checks = await asyncio.to_thread(check_docker, ctl, docker_installed())
    checks += check_gpu()
    probe, brain_checks = await check_brain(paths)
    checks += brain_checks
    checks += check_models(probe)
    checks += await check_sandbox()
    checks += check_packs()
    return checks


async def collect(
    paths: WaddlePaths,
    env: Mapping[str, str],
    base: Path | None = None,
    ctl: StatusSource | None = None,
) -> list[Check]:
    base = base or data_base()
    data_home = paths.data_dir.parent
    checks = check_session(env)
    config, config_checks = check_config(paths)
    checks += config_checks
    checks += check_files(paths, data_home, base)
    checks += await check_helper(paths)
    if config is not None:
        checks += await check_live(config)
    checks += await collect_phase3(paths, ctl)
    return checks


def render(checks: list[Check]) -> str:
    lines: list[str] = []
    area = ""
    for check in checks:
        if check.area != area:
            area = check.area
            lines.append(f"{area}:")
        lines.append(f"  {_MARK[check.level]} {check.text}")
        if check.fix:
            lines.append(f"      → {check.fix}")
    bad = sum(1 for c in checks if c.level == BAD)
    warn = sum(1 for c in checks if c.level == WARN)
    skipped = sum(1 for c in checks if c.level == SKIP)
    lines.append("")
    if not (bad or warn or skipped):
        lines.append("All good.")
    else:
        unchecked = f", {skipped} not checked" if skipped else ""
        lines.append(f"{bad} problem(s), {warn} warning(s){unchecked}.")
    return "\n".join(lines)


def run(paths: WaddlePaths) -> int:
    checks = sorted(asyncio.run(collect(paths, os.environ)), key=lambda c: _ORDER.index(c.area))
    print(render(checks))
    return 1 if any(c.level == BAD for c in checks) else 0


_ORDER = [
    "session",
    "config",
    "kwin",
    "capture",
    "privacy",
    "hotkeys",
    "docker",
    "gpu",
    "brain",
    "models",
    "sandbox",
    "apps",
]
__all__ = ["Check", "collect", "render", "run"]
