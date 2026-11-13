"""Structural guards for the prime directive (ARCHITECTURE §1, invariants I1 and I2).

These scan the shipped source, so a forbidden capability cannot arrive unnoticed in a
later change. They are deliberately blunt: a false positive is fixed by rewording a comment.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE_DIRS = [
    REPO / "packages" / "core" / "src",
    REPO / "packages" / "desktop" / "src",
    REPO / "packages" / "brain" / "src",
    REPO / "kwin",
    REPO / "capture-helper" / "src",
]
SUFFIXES = {".py", ".qml", ".js", ".rs", ".sh", ".toml"}

# Input injection, raw input devices, and remote-control portals. Never, in any form.
FORBIDDEN = re.compile(
    r"uinput|ydotool|xdotool|\bwtype\b|\bdotool\b|/dev/input|\blibei\b|evdev|"
    r"RemoteDesktop|InputCapture|xtest|fake_input|sendEvent\(|postEvent\(",
    re.IGNORECASE,
)
# The clipboard may only ever be *written*, by an explicit Copy button (not built yet).
CLIPBOARD = re.compile(r"clipboard", re.IGNORECASE)
# The only modules allowed to start processes: each is an allowlist in its own right.
# own_exec: Waddle's own window rule. helper_proc: Waddle's own capture helper, nothing else.
# readonly_exec: the sandboxed observers (man, --version). brainctl/compose: Waddle's own
# containers through a fixed list of docker compose calls.
SUBPROCESS_ALLOWED = {
    "placement/own_exec.py",
    "sensors/helper_proc.py",
    "sandbox/readonly_exec.py",
    "brainctl/compose.py",
}


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
    assert not hits, "clipboard referenced (only a future Copy button may write it):\n" + "\n".join(
        hits
    )


_SPAWN_ATTR = re.compile(
    r"^(create_subprocess_(exec|shell)|subprocess_(exec|shell)|startDetached|system|popen|"
    r"exec[lv]p?e?|spawn\w*|posix_spawn\w*|fork\w*)$"
)
_SPAWN_MODULES = {"subprocess", "pty", "multiprocessing", "ctypes", "cffi"}
_SPAWN_NAMES = {"QProcess", "QDesktopServices", "QTest"}


def spawn_references(tree: ast.AST) -> list[str]:
    """Every way this module could start a process or reach native code, by name."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in _SPAWN_MODULES]
            found += [a.name for a in node.names if a.name.startswith("asyncio.subprocess")]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {a.name for a in node.names}
            if module.split(".")[0] in _SPAWN_MODULES and not names <= {"CompletedProcess"}:
                found.append(module)  # importing only the result *type* cannot start a process
            if module == "asyncio" and "subprocess" in names:
                found.append("asyncio.subprocess")
            found += [f"{module}.{n}" for n in names if n in _SPAWN_NAMES or _SPAWN_ATTR.match(n)]
        elif isinstance(node, ast.Attribute) and _SPAWN_ATTR.match(node.attr):
            found.append(f".{node.attr}")  # whatever the base is called (covers aliased os)
        elif isinstance(node, ast.Name) and node.id in _SPAWN_NAMES:
            found.append(node.id)
    return found


def test_processes_are_only_started_by_the_allowlisted_modules() -> None:
    offenders = []
    for path in shipped_files():
        if path.suffix != ".py":
            continue
        rel = path.as_posix().split("/waddle_desktop/")[-1]
        if rel in SUBPROCESS_ALLOWED:
            continue
        refs = spawn_references(ast.parse(path.read_text(encoding="utf-8")))
        offenders += [f"{path.relative_to(REPO)}: {ref}" for ref in refs]
    assert not offenders, "process creation outside the exec allowlists:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "def f():\n    import pty",
        "import asyncio\nasyncio.create_subprocess_exec('x')",
        "loop.subprocess_exec(P, 'x')",
        "from asyncio import subprocess",
        "import os as o\no.system('x')",
        "from os import execv",
        "import os\nos.posix_spawn('x', [], {})",
        "import os\nos.fork()",
        "from PySide6.QtCore import QProcess",
        "QProcess.startDetached('x')",
        "from PySide6.QtGui import QDesktopServices",
        "import ctypes",
        "from PySide6.QtTest import QTest",
    ],
)
def test_the_process_guard_catches_each_way_in(source: str) -> None:
    assert spawn_references(ast.parse(source)), f"guard missed: {source!r}"


@pytest.mark.parametrize(
    "source",
    ["app.exec()", "from subprocess import CompletedProcess", "os.kill(pid, 15)", "x.execute()"],
)
def test_the_process_guard_allows_what_is_harmless(source: str) -> None:
    assert spawn_references(ast.parse(source)) == []


# A KWin script may set exactly one property, and only on Waddle's own windows.
_JS_FORBIDDEN = re.compile(
    r"\.(minimized|fullScreen|keepAbove|keepBelow|onAllDesktops|desktops|opacity|noBorder|"
    r"skipTaskbar|skipPager|skipSwitcher|shade|activeWindow|currentDesktop)"
    r"\s*=(?!=)|closeWindow|killWindow|sendClientToScreen|slotWindow|\beval\s*\(|Function\s*\("
)
_JS_GEOMETRY_WRITE = re.compile(r"\.(frameGeometry|clientGeometry)\s*=(?!=)")


def test_kwin_scripts_only_assign_geometry_behind_the_own_window_guard() -> None:
    """Every KWin script that writes a window property must contain the ownership guard."""
    js = [p for p in shipped_files() if p.suffix == ".js"]
    templates = [p for p in shipped_files() if p.name == "scripts.py"]
    assert js and templates
    for path in js + templates:
        text = path.read_text(encoding="utf-8")
        if _JS_GEOMETRY_WRITE.search(text):
            assert "isOwnWindow" in text, (
                f"{path.relative_to(REPO)} moves windows without the guard"
            )
        hit = _JS_FORBIDDEN.search(text)
        assert hit is None, f"{path.relative_to(REPO)} uses {hit.group(0) if hit else ''}"


@pytest.mark.parametrize(
    "snippet",
    [
        "w.minimized = true",
        "w.keepAbove=false",
        "w.fullScreen  = true",
        "workspace.activeWindow = w",
        "w.closeWindow()",
        "eval (x)",
        "new Function('x')",
    ],
)
def test_the_kwin_guard_catches_state_changes(snippet: str) -> None:
    assert _JS_FORBIDDEN.search(snippet)


def test_the_kwin_guard_allows_reads_and_comparisons() -> None:
    for snippet in ("if (w.minimized == true)", "var a = w.fullScreen;", "w.output === o"):
        assert not _JS_FORBIDDEN.search(snippet)
