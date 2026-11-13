"""Basic system numbers for ``get_system_stats`` (ARCHITECTURE §4.8, §5.2).

Plain synchronous readers of ``/proc`` and ``statvfs``. Each returns a small JSON-safe dict
of numbers and short strings. Nothing here names a process, a command line, a mount or a
path: the two filesystems reported are labelled ``"/"`` and ``"home"``.

``root`` is where ``proc`` is found; tests point it at a fixture directory.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from waddle_core.untrusted import clean

Topic = Literal["cpu", "memory", "disk", "gpu", "uptime"]
Stats = dict[str, object]
StatVfs = Callable[[str], os.statvfs_result]

MODEL_NAME_CHARS: Final = 80
_MAX_PROC_BYTES: Final = 8 * 1024 * 1024
_KIB: Final = 1024


def system_stats(
    topic: Topic,
    *,
    root: Path = Path("/"),
    home: Path | None = None,
    statvfs: StatVfs = os.statvfs,
) -> Stats:
    """The numbers for one topic, or ``{"available": False, ...}`` if they cannot be read."""
    try:
        if topic == "cpu":
            return cpu(root)
        if topic == "memory":
            return memory(root)
        if topic == "disk":
            return disk(Path.home() if home is None else home, statvfs=statvfs)
        if topic == "uptime":
            return uptime(root)
        if topic == "gpu":
            return gpu()
        # Callers validate the topic first; a wrong one must still not read as "no GPU".
        return {"available": False, "reason": "unknown topic"}  # type: ignore[unreachable]
    except (OSError, ValueError, ArithmeticError, KeyError, IndexError):
        return {"available": False, "reason": "could not be read"}


def cpu(root: Path = Path("/")) -> Stats:
    """Model name, logical core count and the three load averages."""
    model, cores = "", 0
    for line in _read(root, "cpuinfo").splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "processor":
            cores += 1
        elif key.strip() == "model name" and not model:
            model = clean(" ".join(value.split()), MODEL_NAME_CHARS)
    load = [float(field) for field in _read(root, "loadavg").split()[:3]]
    if len(load) != 3 or not all(math.isfinite(value) for value in load):
        raise ValueError("loadavg")  # "nan" and "inf" parse as floats but are not JSON
    return {
        "model": model or "unknown",
        "cores": cores or (os.cpu_count() or 1),
        "load_1m": load[0],
        "load_5m": load[1],
        "load_15m": load[2],
    }


def memory(root: Path = Path("/")) -> Stats:
    """Total and available memory and swap, in bytes."""
    fields: dict[str, int] = {}
    for line in _read(root, "meminfo").splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if parts and parts[0].isdecimal():
            fields[key.strip()] = int(parts[0]) * _KIB  # meminfo counts in kB
    return {
        "total_bytes": fields["MemTotal"],
        "available_bytes": fields["MemAvailable"],
        "swap_total_bytes": fields.get("SwapTotal", 0),
        "swap_free_bytes": fields.get("SwapFree", 0),
    }


def disk(home: Path, *, statvfs: StatVfs = os.statvfs) -> Stats:
    """Size and free space of the root filesystem and of the one holding the home directory."""
    return {"/": _filesystem(statvfs("/")), "home": _filesystem(statvfs(str(home)))}


def uptime(root: Path = Path("/")) -> Stats:
    """Seconds since boot."""
    return {"seconds": int(float(_read(root, "uptime").split()[0]))}


def gpu() -> Stats:
    """Placeholder: graphics card readings are not built yet."""
    return {"available": False, "reason": "arrives in a later phase"}


def _filesystem(fs: os.statvfs_result) -> Stats:
    # f_bavail is what an ordinary user can still write, which is the number people mean.
    return {"size_bytes": fs.f_blocks * fs.f_frsize, "free_bytes": fs.f_bavail * fs.f_frsize}


def _read(root: Path, name: str) -> str:
    with (root / "proc" / name).open("rb") as file:
        return file.read(_MAX_PROC_BYTES).decode("utf-8", "replace")
