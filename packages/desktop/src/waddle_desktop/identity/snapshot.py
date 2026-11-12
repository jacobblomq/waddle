"""Side-effect-free reads of one process from /proc (ARCHITECTURE §4.6, step 1).

Everything under /proc/<pid> is untrusted: another process chooses its own command line,
name and environment. Reads are capped, nothing raises, and of the environment only a fixed
allowlist of packaging variables is ever kept: the raw block never leaves ``_environment``.
"""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

ENV_ALLOWLIST: Final = frozenset(
    {
        "SNAP",
        "SNAP_NAME",
        "SNAP_VERSION",
        "SNAP_REVISION",
        "SNAP_INSTANCE_NAME",
        "APPIMAGE",
        "APPDIR",
        "FLATPAK_ID",
        "container",
        "BAMF_DESKTOP_FILE_HINT",
        "GIO_LAUNCHED_DESKTOP_FILE",
        "ELECTRON_RUN_AS_NODE",
    }
)
_ENV_KEYS: Final = frozenset(name.encode("ascii") for name in ENV_ALLOWLIST)

MAX_ARGS: Final = 64
MAX_ARG_BYTES: Final = 4096
MAX_CGROUP_BYTES: Final = 8 * 1024
MAX_FLATPAK_INFO_BYTES: Final = 16 * 1024
MAX_ENVIRON_BYTES: Final = 2 * 1024 * 1024
MAX_ENV_VALUE_BYTES: Final = 4096
MAX_STAT_BYTES: Final = 4096
MAX_PID: Final = 2**22  # the kernel's PID_MAX_LIMIT
_DELETED: Final = " (deleted)"
_READ_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY | os.O_NOFOLLOW


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    ppid: int
    exe: str | None
    cmdline: tuple[str, ...]
    env: Mapping[str, str]  # only keys from ENV_ALLOWLIST
    cgroup: str
    root_flatpak_info: str | None
    exe_stat: tuple[int, int] | None  # (size, mtime_ns)


def _read(dir_fd: int, name: str, cap: int) -> bytes | None:
    """At most ``cap`` bytes of a file inside the process directory; None when unreadable.

    Only a regular file is read, and a link in the last place is not followed. Every procfs
    file read here is one, and so is a real ``.flatpak-info``; but a process chooses what
    its own ``/.flatpak-info`` is, and reading a FIFO would take data from its real reader,
    while a link could name any file of the user's.
    """
    try:
        # Checked before the open as well: opening a FIFO or a device is already an event
        # for whoever is on the other side.
        if not stat_module.S_ISREG(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            return None
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except (OSError, ValueError):
        return None
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        remaining = cap
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _ppid(stat: bytes) -> int | None:
    # "pid (comm) state ppid ...": comm is chosen by the process and may contain spaces and
    # parentheses, so only what follows the LAST ')' can be trusted to be fields.
    close = stat.rfind(b")")
    if close < 0:
        return None
    fields = stat[close + 1 :].split()
    if len(fields) < 2:
        return None
    try:
        ppid = int(fields[1])
    except ValueError:
        return None
    return ppid if 0 <= ppid <= MAX_PID else None


def _cmdline(data: bytes) -> tuple[str, ...]:
    if data.endswith(b"\0"):
        data = data[:-1]
    if not data:
        return ()
    return tuple(_text(arg[:MAX_ARG_BYTES]) for arg in data.split(b"\0", MAX_ARGS)[:MAX_ARGS])


def _environment(dir_fd: int) -> Mapping[str, str]:
    """The allowlisted packaging variables. The raw block is dropped before returning."""
    raw = _read(dir_fd, "environ", MAX_ENVIRON_BYTES)
    kept: dict[str, str] = {}
    if raw:
        entries = raw.split(b"\0")
        if len(raw) >= MAX_ENVIRON_BYTES:
            entries.pop()  # possibly cut in the middle of a value
        for entry in entries:
            key, separator, value = entry.partition(b"=")
            if separator and key in _ENV_KEYS and len(value) <= MAX_ENV_VALUE_BYTES:
                kept.setdefault(key.decode("ascii"), _text(value))
        del raw, entries
    return MappingProxyType(kept)


def _exe(dir_fd: int) -> tuple[str | None, tuple[int, int] | None]:
    try:
        # Path.readlink has no dir_fd form; the descriptor pins the process (see read()).
        # Read as bytes: a path that is not UTF-8 must not become a string with lone
        # surrogates, which cannot be encoded again (sent, stored or logged) later.
        target = _text(os.readlink(b"exe", dir_fd=dir_fd))
    except (OSError, ValueError):
        return None, None
    if target.endswith(_DELETED):
        target = target[: -len(_DELETED)]
    exe = target if 0 < len(target) <= MAX_ARG_BYTES else None
    try:
        # Following the kernel's link works even for a deleted or sandboxed executable.
        status = os.stat("exe", dir_fd=dir_fd)
    except (OSError, ValueError):
        return exe, None
    return exe, (status.st_size, status.st_mtime_ns)


class ProcReader:
    """Reads process snapshots. Opens files read-only and never writes or signals."""

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self._root = proc_root

    def read(self, pid: int) -> ProcessSnapshot | None:
        """A snapshot of ``pid``; None when it is gone or unreadable. Never raises."""
        if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= MAX_PID:
            return None
        try:
            # Every read goes through this descriptor, so a PID that is recycled while we
            # read cannot mix two processes into one snapshot: the reads just fail.
            dir_fd = os.open(self._root / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except (OSError, ValueError):
            return None
        try:
            return self._snapshot(pid, dir_fd)
        except (OSError, ValueError):  # belt and braces: the helpers already swallow these
            return None
        finally:
            os.close(dir_fd)

    def _snapshot(self, pid: int, dir_fd: int) -> ProcessSnapshot | None:
        stat = _read(dir_fd, "stat", MAX_STAT_BYTES)
        ppid = _ppid(stat) if stat else None
        if ppid is None:
            return None
        exe, exe_stat = _exe(dir_fd)
        # Assumption, not verified on the target (no S2.x spike covers /proc; needs a
        # Phase 2 check): /proc/<pid>/root/.flatpak-info is readable for a same-user
        # Flatpak app, as it is for xdg-desktop-portal.
        info = _read(dir_fd, "root/.flatpak-info", MAX_FLATPAK_INFO_BYTES)
        return ProcessSnapshot(
            pid=pid,
            ppid=ppid,
            exe=exe,
            cmdline=_cmdline(_read(dir_fd, "cmdline", MAX_ARGS * (MAX_ARG_BYTES + 1)) or b""),
            env=_environment(dir_fd),
            cgroup=_text(_read(dir_fd, "cgroup", MAX_CGROUP_BYTES) or b""),
            root_flatpak_info=None if info is None else _text(info),
            exe_stat=exe_stat,
        )

    def chain(self, pid: int, max_depth: int = 8) -> list[ProcessSnapshot]:
        """``pid`` and its ancestors, nearest first; stops at pid 1, a loop, or the depth."""
        found: list[ProcessSnapshot] = []
        seen: set[int] = set()
        current = pid
        while len(found) < max_depth and current not in seen:
            seen.add(current)
            snapshot = self.read(current)
            if snapshot is None:
                break
            found.append(snapshot)
            if snapshot.ppid <= 1:
                break
            current = snapshot.ppid
        return found
