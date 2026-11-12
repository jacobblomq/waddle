"""``waddle doctor``: what works on this machine, what does not, and what to do about it.

Read-only. It asks the session a few questions, starts Waddle's own capture helper to say
hello, and prints the answers. It captures nothing and changes nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from waddle_core.config import ConfigError, WaddleConfig, load_config
from waddle_core.identity import CAPTURE_APP_ID
from waddle_core.paths import WaddlePaths
from waddle_core.privacy import BlocklistError, load_blocklist
from waddle_desktop.resources import data_base, default_blocklist_path

OK, WARN, BAD = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "!", BAD: "✗"}


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


async def collect(
    paths: WaddlePaths, env: Mapping[str, str], base: Path | None = None
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
    lines.append("")
    lines.append("All good." if not (bad or warn) else f"{bad} problem(s), {warn} warning(s).")
    return "\n".join(lines)


def run(paths: WaddlePaths) -> int:
    checks = sorted(asyncio.run(collect(paths, os.environ)), key=lambda c: _ORDER.index(c.area))
    print(render(checks))
    return 1 if any(c.level == BAD for c in checks) else 0


_ORDER = ["session", "config", "kwin", "capture", "privacy", "hotkeys"]
__all__ = ["Check", "collect", "render", "run"]
