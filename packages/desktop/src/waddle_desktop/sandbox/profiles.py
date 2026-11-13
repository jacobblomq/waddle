"""The one sandbox profile, as a pure argv builder (ARCHITECTURE §4.12, D-043).

Nothing here starts a process. ``sandbox_argv`` returns the full command line that
``readonly_exec`` runs: ``bwrap`` with every namespace unshared, a read-only view of the system
directories, an empty home and ``/tmp``, a cleared environment, and ``prlimit`` *inside* the
sandbox in front of the tool.

``prlimit`` sits inside because the process-count limit is charged per user namespace: set
outside, it would count every process of the desktop user and refuse to start anything.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

SANDBOX_HOME: Final = "/home/sandbox"
SANDBOX_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
DEFAULT_LOCALE: Final = "C.UTF-8"
TMPFS_BYTES: Final = 8 * 1024 * 1024

# What a dynamically linked tool, and man with its formatter, need to start. Only the entries
# that exist on this machine are used. Nothing of the user's is on this list.
SYSTEM_PATHS: Final = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc/alternatives",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/manpath.config",
    "/etc/man_db.conf",
    "/etc/groff",
    "/var/cache/man",
)
# Where the common tldr clients keep their downloaded pages, relative to the home directory.
# The pages are public text; they are shown read-only so a client can answer without network.
TLDR_CACHES: Final = (".cache/tealdeer", ".cache/tldr", ".cache/tlrc", ".tldrc", ".tldr")

_LOCALE: Final = re.compile(r"(C|POSIX|[a-z]{2,3}(_[A-Z]{2})?)([.](UTF-8|utf8))?(@[a-z]{1,16})?")
_ENV_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{0,31}")


@dataclass(frozen=True)
class Limits:
    """Resource limits applied to the tool by ``prlimit``."""

    address_space_bytes: int
    cpu_seconds: int
    file_size_bytes: int
    processes: int


def safe_locale(*candidates: str | None) -> str:
    """The first candidate that is a plain locale name, or ``C.UTF-8``.

    Locale variables come from the user's session; only names of a known shape are passed on.
    """
    for value in candidates:
        if value and _LOCALE.fullmatch(value):
            return value
    return DEFAULT_LOCALE


def system_binds(root: Path = Path("/")) -> list[str]:
    """bwrap options that show the system directories read-only.

    A top-level symlink such as ``/bin -> usr/bin`` is recreated as a symlink. ``root`` is
    where to look for the entries (a fixture directory in tests); the options always name the
    real absolute paths.
    """
    options: list[str] = []
    for path in SYSTEM_PATHS:
        here = root / path.lstrip("/")
        if here.is_symlink():
            options += ["--symlink", str(here.readlink()), path]
        elif here.exists():
            options += ["--ro-bind", path, path]
    return options


def probe_binds(real_exe: PurePosixPath) -> list[tuple[str, str]]:
    """Read-only binds an executable needs beyond the system directories.

    Under ``/usr`` nothing more is needed. An app under ``/opt`` gets its own top-level
    directory (it loads libraries from next to itself). Anything else gets the one file only,
    so probing can never expose a home directory.
    """
    parts = real_exe.parts
    if not real_exe.is_absolute() or ".." in parts:
        raise ValueError("executable path must be absolute and normalised")
    if parts[:2] == ("/", "usr"):
        return []
    if parts[:2] == ("/", "opt") and len(parts) > 2:
        app_dir = str(PurePosixPath(*parts[:3]))
        return [(app_dir, app_dir)]
    return [(str(real_exe), str(real_exe))]


def tldr_cache_binds(home: Path) -> list[tuple[str, str]]:
    """Read-only binds of the tldr page caches that exist, placed in the sandbox home.

    A cache is used only if it is a real directory inside ``home`` (a symlink that points
    elsewhere is ignored).
    """
    try:
        real_home = home.resolve(strict=True)
    except OSError:
        return []
    binds: list[tuple[str, str]] = []
    for rel in TLDR_CACHES:
        try:
            real = (real_home / rel).resolve(strict=True)
        except OSError:
            continue
        if real == real_home / rel and real.is_dir():
            binds.append((str(real), f"{SANDBOX_HOME}/{rel}"))
    return binds


def sandbox_argv(
    *,
    bwrap: str,
    prlimit: str,
    command: Sequence[str],
    limits: Limits,
    locale: str = DEFAULT_LOCALE,
    env: Mapping[str, str] | None = None,
    ro_binds: Sequence[tuple[str, str]] = (),
    root: Path = Path("/"),
) -> list[str]:
    """The complete argv: ``bwrap <profile> -- prlimit <limits> -- <command>``.

    The sandbox has no network, no D-Bus or Wayland socket, no runtime directory and no
    variable from the caller's environment. ``env`` adds fixed variables for the tool (pager
    settings); ``ro_binds`` adds ``(source, destination)`` read-only binds.
    """
    if not command:
        raise ValueError("command is empty")
    variables = {
        "PATH": SANDBOX_PATH,
        "HOME": SANDBOX_HOME,
        "LANG": safe_locale(locale),
        "LC_ALL": safe_locale(locale),
        "TERM": "dumb",
        **(env or {}),
    }
    # bwrap keeps capabilities when its caller is root; dropped, a tool can never remount the
    # read-only system writable, whoever runs Waddle.
    argv = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL"]
    argv += ["--clearenv"]
    for name, value in variables.items():
        if not _ENV_NAME.fullmatch(name) or "\0" in value:
            raise ValueError("bad environment variable")
        argv += ["--setenv", name, value]
    argv += system_binds(root)
    argv += ["--proc", "/proc", "--dev", "/dev"]
    for mount in ("/tmp", SANDBOX_HOME, "/dev/shm"):  # noqa: S108 - inside the sandbox
        argv += ["--size", str(TMPFS_BYTES), "--tmpfs", mount]
    for source, destination in ro_binds:
        argv += ["--ro-bind", source, destination]
    # The sandbox root and /dev are memory-backed and as large as half the machine's memory.
    # Read-only, the three small mounts above are the only places a tool can write.
    argv += ["--remount-ro", "/dev", "--remount-ro", "/"]
    argv += ["--chdir", "/", "--"]
    argv += [
        prlimit,
        f"--as={limits.address_space_bytes}",
        f"--cpu={limits.cpu_seconds}",
        f"--fsize={limits.file_size_bytes}",
        f"--nproc={limits.processes}",
        # A probed program that crashes writes no core file. UNVERIFIED (spike S3.5): where
        # the system pipes crashes to its own collector (apport, systemd-coredump), the
        # kernel hands the crash over whatever this limit says; what that collector then
        # keeps is the system's doing and has not been looked at on the target machine.
        "--core=0",
        "--",
    ]
    return [*argv, *command]
