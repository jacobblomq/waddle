"""The own-resources exec guard: only three argv templates ever reach a process (I2)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from waddle_desktop.placement import own_exec
from waddle_desktop.placement.own_exec import KWIN_RECONFIGURE, OwnExecError, run_own, validate

FAKE = str(Path(__file__).with_name("fake_kwriteconfig6.py"))
RESOLVER = {
    "kwriteconfig6": [sys.executable, "-I", "-S", FAKE, "kwriteconfig6"],
    "kreadconfig6": [sys.executable, "-I", "-S", FAKE, "kreadconfig6"],
    "dbus-send": [sys.executable, "-c", "import sys; print(' '.join(sys.argv[1:]))"],
}
RC = "/home/u/.config/kwinrulesrc"


def kwrite(*tail: str, file: str = RC, group: str = "waddle-penguin", key: str = "above") -> list:
    return ["kwriteconfig6", "--file", file, "--group", group, "--key", key, *tail]


def kread(*tail: str, file: str = RC, group: str = "General", key: str = "rules") -> list:
    return ["kreadconfig6", "--file", file, "--group", group, "--key", key, *tail]


REJECTED: dict[str, Any] = {
    "empty argv": [],
    "nested group via brackets": kwrite("x", group="waddle-x][General", key="rules"),
    "group with a slash": kwrite("x", group="waddle-x/General"),
    "group with trailing space": kwrite("x", group="waddle-x "),
    "group with uppercase": kwrite("x", group="waddle-Penguin"),
    "a bare string": "kwriteconfig6",
    "unknown binary": ["rm", "-rf", "/"],
    "shell": ["sh", "-c", "kwriteconfig6"],
    "binary given by path": ["/usr/bin/kwriteconfig6", *kwrite("true")[1:]],
    "relative path": kwrite("true", file=".config/kwinrulesrc"),
    "bare file name": kwrite("true", file="kwinrulesrc"),
    "another config file": kwrite("true", file="/home/u/.config/kwinrc"),
    "kwinrulesrc as a prefix": kwrite("true", file="/home/u/.config/kwinrulesrc.bak"),
    "kwinrulesrc as a directory": kwrite("true", file="/home/u/kwinrulesrc/kdeglobals"),
    "path with dot-dot": kwrite("true", file="/home/u/kwinrulesrc/../kwinrulesrc"),
    "path with trailing slash": kwrite("true", file="/home/u/.config/kwinrulesrc/"),
    "foreign group on write": kwrite("true", group="1f0e7a52-user-rule"),
    "group that only contains our prefix": kwrite("true", group="not-waddle-penguin"),
    "bare prefix as group": kwrite("true", group="waddle-"),
    "General with a foreign key": kwrite("1", group="General", key="Description"),
    "General count not a number": kwrite("many", group="General", key="count"),
    "key that looks like an option": kwrite("true", key="--delete"),
    "key with odd characters": kwrite("true", key="above[$e]"),
    "no value": kwrite(),
    "extra argument after the value": kwrite("true", "extra"),
    "extra argument after --delete": kwrite("--delete", "extra"),
    "value that looks like an option": kwrite("--notify"),
    "unknown type": kwrite("--type", "path", "true"),
    "options reordered": ["kwriteconfig6", "--group", "waddle-x", "--file", RC, "--key", "k", "v"],
    "second --file": kwrite("--file", "/etc/passwd"),
    "newline in value": kwrite("true\n[General]"),
    "carriage return in value": kwrite("true\rx"),
    "NUL in value": kwrite("true\x00"),
    "newline in group": kwrite("true", group="waddle-a\n[General]"),
    "NUL in path": kwrite("true", file="/home/u/\x00/kwinrulesrc"),
    "lone surrogate in value": kwrite("true\ud800"),
    "non-string argument": kwrite(1),  # type: ignore[arg-type]
    "read from another file": kread(file="/home/u/.config/kwinrc"),
    "read with a relative path": kread(file="kwinrulesrc"),
    "read with extra args": kread("--default", "x"),
    "read with newline": kread(key="rules\n"),
    "read missing key": ["kreadconfig6", "--file", RC, "--group", "General"],
    "dbus other destination": [*KWIN_RECONFIGURE[:3], "--dest=org.kde.plasmashell", "/KWin", "x"],
    "dbus other method": [*KWIN_RECONFIGURE[:-1], "org.kde.KWin.killWindow"],
    "dbus other path": [*KWIN_RECONFIGURE[:4], "/Scripting", KWIN_RECONFIGURE[-1]],
    "dbus system bus": ["dbus-send", "--system", *KWIN_RECONFIGURE[2:]],
    "dbus extra argument": [*KWIN_RECONFIGURE, "string:x"],
    "dbus truncated": list(KWIN_RECONFIGURE[:-1]),
}

ACCEPTED: dict[str, list[str]] = {
    "write own group": kwrite("true"),
    "write empty value": kwrite("", key="desktops"),
    "write with type": kwrite("--type", "bool", "true"),
    "write value with spaces and commas": kwrite("Waddle Highlight, 1", key="title"),
    "delete own key": kwrite("--delete"),
    "write General rules": kwrite("a,b,waddle-penguin", group="General", key="rules"),
    "write General count": kwrite("3", group="General", key="count"),
    "delete General count": kwrite("--delete", group="General", key="count"),
    "read General": kread(),
    "read a foreign group": kread(group="some user rule", key="Description"),
    "reconfigure": list(KWIN_RECONFIGURE),
}


class Spy:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        raise AssertionError("a process must not be spawned")


class SpyResolver(dict[str, list[str]]):
    """A resolver that records every lookup (lookups happen only after validation)."""

    lookups: list[str]

    def get(self, key: str, default: Any = None) -> Any:
        self.lookups.append(key)
        return super().get(key, default)


@pytest.mark.parametrize("argv", REJECTED.values(), ids=REJECTED.keys())
def test_rejected_argv_never_spawns(argv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy()
    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr(subprocess, "Popen", spy)
    monkeypatch.setattr(own_exec.shutil, "which", spy)
    resolver = SpyResolver(RESOLVER)
    resolver.lookups = []

    with pytest.raises(OwnExecError):
        run_own(argv)
    with pytest.raises(OwnExecError):
        run_own(argv, resolver=resolver)

    assert spy.calls == []
    assert resolver.lookups == []


@pytest.mark.parametrize("argv", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_accepted_argv_passes_validation(argv: list[str]) -> None:
    validate(argv)


def test_spy_would_catch_a_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the rejection test above: a valid argv does reach subprocess.run."""
    spy = Spy()
    monkeypatch.setattr(subprocess, "run", spy)
    with pytest.raises(AssertionError, match="must not be spawned"):
        run_own(list(KWIN_RECONFIGURE), resolver=RESOLVER)
    (args, kwargs) = spy.calls[0]
    assert kwargs["shell"] is False
    assert args[0][-5:] == list(KWIN_RECONFIGURE[1:])


def test_write_read_delete_roundtrip_through_the_double(tmp_path: Path) -> None:
    rc = str(tmp_path / "kwinrulesrc")
    assert run_own(kwrite("true", file=rc), resolver=RESOLVER).returncode == 0
    assert run_own(kwrite("", file=rc, key="desktops"), resolver=RESOLVER).returncode == 0
    assert Path(rc).read_text() == "[waddle-penguin]\nabove=true\ndesktops=\n"

    result = run_own(kread(file=rc, group="waddle-penguin", key="above"), resolver=RESOLVER)
    assert (result.returncode, result.stdout) == (0, "true\n")

    for key in ("above", "desktops"):
        run_own(kwrite("--delete", file=rc, key=key), resolver=RESOLVER)
    assert Path(rc).read_text() == ""


def test_double_breaks_lines_only_at_newlines(tmp_path: Path) -> None:
    rc = tmp_path / "kwinrulesrc"
    rc.write_text("[user]\nname=a\u2028b\x0cc\n\n", encoding="utf-8")
    odd = "x\u2028y\x85z"
    assert run_own(kwrite(odd, file=str(rc), key="title"), resolver=RESOLVER).returncode == 0
    result = run_own(kread(file=str(rc), group="waddle-penguin", key="title"), resolver=RESOLVER)
    assert result.stdout == odd + "\n"
    run_own(kwrite("--delete", file=str(rc), key="title"), resolver=RESOLVER)
    assert rc.read_text(encoding="utf-8") == "[user]\nname=a\u2028b\x0cc\n\n"


def test_reconfigure_template_is_passed_through_unchanged() -> None:
    result = run_own(list(KWIN_RECONFIGURE), resolver=RESOLVER)
    assert result.stdout.split() == list(KWIN_RECONFIGURE[1:])


def test_environment_is_reduced_to_the_passthrough_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WADDLE_SECRET_TOKEN", "hunter2")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    dump_env = [sys.executable, "-c", "import os; print(sorted(os.environ))"]
    out = run_own(list(KWIN_RECONFIGURE), resolver={"dbus-send": dump_env}).stdout
    assert "DBUS_SESSION_BUS_ADDRESS" in out
    assert "WADDLE_SECRET_TOKEN" not in out


def test_missing_program_is_an_own_exec_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(OwnExecError, match="not available"):
        run_own(list(KWIN_RECONFIGURE), resolver={})
    monkeypatch.setattr(own_exec.shutil, "which", lambda _name: None)
    with pytest.raises(OwnExecError, match="not available"):
        run_own(list(KWIN_RECONFIGURE))


def test_default_resolver_uses_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(own_exec.shutil, "which", lambda _name: "/bin/true")
    assert run_own(list(KWIN_RECONFIGURE)).returncode == 0


def test_timeout_and_unrunnable_program_become_own_exec_errors(tmp_path: Path) -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(OwnExecError, match="TimeoutExpired"):
        run_own(list(KWIN_RECONFIGURE), timeout=0.2, resolver={"dbus-send": sleeper})
    with pytest.raises(OwnExecError, match="could not be run"):
        run_own(list(KWIN_RECONFIGURE), resolver={"dbus-send": [str(tmp_path / "missing")]})
