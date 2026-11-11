"""The only place Waddle runs a state-changing command (ARCHITECTURE §1, invariant I2).

``run_own`` accepts an argv only if it matches one of three fixed templates and refuses
everything else *before* anything is executed:

1. ``kwriteconfig6 --file <abs>/kwinrulesrc --group <g> --key <k> [--type <t>] <value>`` and
   ``... --key <k> --delete``, where ``<g>`` is one of Waddle's own ``waddle-*`` rule groups, or
   ``General`` with the key ``count`` or ``rules`` (the rule index KWin keeps there).
2. ``kreadconfig6 --file <abs>/kwinrulesrc --group <g> --key <k>`` (read-only, any group).
3. ``dbus-send --session --type=method_call --dest=org.kde.KWin /KWin org.kde.KWin.reconfigure``.

There is no shell, the environment is reduced to a short passthrough list, and no argument may
contain a control character.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Final

RULES_BASENAME: Final = "kwinrulesrc"
OWN_GROUP_PREFIX: Final = "waddle-"

KWIN_RECONFIGURE: Final[tuple[str, ...]] = (
    "dbus-send",
    "--session",
    "--type=method_call",
    "--dest=org.kde.KWin",
    "/KWin",
    "org.kde.KWin.reconfigure",
)

_ENV_PASSTHROUGH: Final = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "LANG",
    "LC_ALL",
)
_OWN_GROUP: Final = re.compile(r"waddle-[a-z0-9-]{1,48}")
_KEY: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_GENERAL_KEYS: Final = frozenset({"count", "rules"})
_VALUE_TYPES: Final = frozenset({"string", "bool"})


class OwnExecError(Exception):
    """The argv is not on the allowlist, or the allowed command could not be run."""


def run_own(
    argv: Sequence[str],
    *,
    timeout: float = 5.0,
    resolver: Mapping[str, Sequence[str]] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an allowlisted command and return its result (the exit status is not checked).

    ``resolver`` replaces the ``PATH`` lookup: it maps a program name to the argv prefix that
    runs it. Tests use it to substitute a double; a name missing from it is "not found".
    """
    validate(argv)
    program = argv[0]
    if resolver is None:
        found = shutil.which(program)
        prefix: Sequence[str] = [found] if found else []
    else:
        prefix = resolver.get(program, [])
    if not prefix:
        raise OwnExecError(f"{program} is not available")
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    try:
        return subprocess.run(  # noqa: S603 - argv was checked against the allowlist above
            [*prefix, *argv[1:]],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OwnExecError(f"{program} could not be run: {type(exc).__name__}") from exc


def validate(argv: Sequence[str]) -> None:
    """Raise ``OwnExecError`` unless ``argv`` matches one of the templates in the module doc."""
    if isinstance(argv, str) or not argv:
        raise OwnExecError("argv must be a non-empty sequence of strings")
    for arg in argv:
        if not isinstance(arg, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in arg):
            raise OwnExecError("argv contains a non-string or a control character")
        try:
            arg.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate cannot be passed to a process
            raise OwnExecError("argv contains text that is not valid Unicode") from None
    program, args = argv[0], list(argv[1:])
    if program == "dbus-send":
        if tuple(argv) != KWIN_RECONFIGURE:
            raise OwnExecError("only org.kde.KWin.reconfigure may be called")
    elif program == "kreadconfig6":
        if _config_tail(args) != []:
            raise OwnExecError("kreadconfig6 takes --file, --group and --key only")
    elif program == "kwriteconfig6":
        tail = _config_tail(args)
        _check_own_entry(group=args[3], key=args[5])
        if tail == ["--delete"]:
            return
        if len(tail) == 3 and tail[0] == "--type" and tail[1] in _VALUE_TYPES:
            tail = tail[2:]
        # A value starting with "-" would be read as an option by the real tool.
        if len(tail) != 1 or tail[0].startswith("-"):
            raise OwnExecError("kwriteconfig6 takes exactly one value, or --delete")
        if (args[3], args[5]) == ("General", "count") and not tail[0].isdecimal():
            raise OwnExecError("General/count must be a number")
    else:
        raise OwnExecError("program is not on the own-resources allowlist")


def _config_tail(args: list[str]) -> list[str]:
    """Check the shared ``--file P --group G --key K`` head; return what follows it."""
    if len(args) < 6 or (args[0], args[2], args[4]) != ("--file", "--group", "--key"):
        raise OwnExecError("expected --file <path> --group <group> --key <key>")
    path, group, key = args[1], args[3], args[5]
    if (
        not PurePosixPath(path).is_absolute()
        or os.path.normpath(path) != path  # no "..", no trailing slash
        or PurePosixPath(path).name != RULES_BASENAME
    ):
        raise OwnExecError(f"--file must be a normalised absolute path to {RULES_BASENAME}")
    if not group or not key or group.startswith("-") or key.startswith("-"):
        raise OwnExecError("group and key must be plain names")
    return args[6:]


def _check_own_entry(*, group: str, key: str) -> None:
    if group == "General":
        if key not in _GENERAL_KEYS:
            raise OwnExecError("in [General] only count and rules may be written")
    elif not _OWN_GROUP.fullmatch(group):
        raise OwnExecError(f"only {OWN_GROUP_PREFIX}* groups may be written")
    elif not _KEY.fullmatch(key):
        raise OwnExecError("key is not a plain rule key")
