"""Structural guards for the prime directive (ARCHITECTURE §1, invariants I1 and I2).

These scan the shipped source, so a forbidden capability cannot arrive unnoticed in a
later change. They are deliberately blunt: a false positive is fixed by rewording a comment.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE_DIRS = [REPO / "packages" / "core" / "src", REPO / "packages" / "desktop" / "src", REPO / "kwin"]
SUFFIXES = {".py", ".qml", ".js", ".rs", ".sh", ".toml"}

# Input injection, raw input devices, and remote-control portals. Never, in any form.
FORBIDDEN = re.compile(
    r"uinput|ydotool|xdotool|wtype|dotool|/dev/input|libei\b|libevdev|evdev|"
    r"RemoteDesktop|InputCapture|xtest|fake_input|sendEvent\(|postEvent\(",
    re.IGNORECASE,
)
# The clipboard may only ever be *written*, by an explicit Copy button (not built yet).
CLIPBOARD = re.compile(r"clipboard", re.IGNORECASE)
# The only modules allowed to start processes: each is an allowlist in its own right.
SUBPROCESS_ALLOWED = {"placement/own_exec.py"}


def shipped_files() -> list[Path]:
    files = [
        p
        for d in SOURCE_DIRS
        if d.exists()
        for p in d.rglob("*")
        if p.is_file() and p.suffix in SUFFIXES and "__pycache__" not in p.parts
    ]
    assert files, "source tree not found"
    return files


def test_no_input_injection_or_raw_input_anywhere() -> None:
    hits = [
        f"{p.relative_to(REPO)}:{n}: {line.strip()}"
        for p in shipped_files()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN.search(line)
    ]
    assert not hits, "forbidden capability referenced:\n" + "\n".join(hits)


def test_clipboard_is_not_touched() -> None:
    hits = [
        f"{p.relative_to(REPO)}:{n}"
        for p in shipped_files()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if CLIPBOARD.search(line)
    ]
    assert not hits, "clipboard referenced (only a future Copy button may write it):\n" + "\n".join(hits)


def test_processes_are_only_started_by_the_allowlisted_modules() -> None:
    offenders = []
    for path in shipped_files():
        if path.suffix != ".py":
            continue
        rel = path.as_posix().split("/waddle_desktop/")[-1]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "os" and node.attr in {"system", "popen", "execv", "execve"}:
                    names = ["os." + node.attr]
                if node.value.id == "os" and node.attr.startswith(("spawn", "posix_spawn", "fork")):
                    names = ["os." + node.attr]
            for name in names:
                root = name.split(".")[0]
                if root in {"subprocess", "pty", "multiprocessing"} or name.startswith("os."):
                    if rel not in SUBPROCESS_ALLOWED:
                        offenders.append(f"{path.relative_to(REPO)}: {name}")
                if name.startswith("asyncio.subprocess"):
                    offenders.append(f"{path.relative_to(REPO)}: {name}")
    assert not offenders, "process creation outside the exec allowlists:\n" + "\n".join(offenders)


def test_kwin_scripts_only_assign_geometry_behind_the_own_window_guard() -> None:
    """Every KWin script that writes a window property must contain the ownership guard."""
    js = [p for p in shipped_files() if p.suffix == ".js"]
    py_templates = [p for p in shipped_files() if p.name == "scripts.py"]
    for path in js + py_templates:
        text = path.read_text(encoding="utf-8")
        if re.search(r"\.frameGeometry\s*=[^=]", text):
            assert "isOwnWindow" in text, f"{path.relative_to(REPO)} moves windows without the guard"
        for forbidden in ("closeWindow", ".minimized =", ".fullScreen =", ".keepAbove =", "killWindow"):
            assert forbidden not in text, f"{path.relative_to(REPO)} uses {forbidden}"
