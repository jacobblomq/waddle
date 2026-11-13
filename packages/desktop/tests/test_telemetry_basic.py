"""System numbers from /proc fixtures, and a smoke test against the real machine."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import get_args

import pytest

from waddle_desktop.telemetry import basic

CPUINFO = """\
processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Intel(R) Core(TM) i7-9700K   CPU @ 3.60GHz
flags\t\t: fpu vme

processor\t: 1
model name\t: Intel(R) Core(TM) i7-9700K   CPU @ 3.60GHz

processor\t: 2
model name\t: Something Else
"""
MEMINFO = """\
MemTotal:       32780000 kB
MemFree:         1000000 kB
MemAvailable:   20000000 kB
SwapTotal:       8000000 kB
SwapFree:        7999000 kB
HugePages_Total:       0
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "cpuinfo").write_text(CPUINFO)
    (proc / "loadavg").write_text("0.52 1.25 2.00 3/1234 56789\n")
    (proc / "meminfo").write_text(MEMINFO)
    (proc / "uptime").write_text("93784.51 700000.12\n")
    return tmp_path


def fake_statvfs(path: str) -> os.statvfs_result:
    blocks, free, available = (1000, 500, 400) if path == "/" else (9000, 3000, 2500)
    return os.statvfs_result((4096, 4096, blocks, free, available, 0, 0, 0, 0, 255))


def test_cpu(root: Path) -> None:
    assert basic.cpu(root) == {
        "model": "Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz",
        "cores": 3,
        "load_1m": 0.52,
        "load_5m": 1.25,
        "load_15m": 2.0,
    }


def test_cpu_model_is_cleaned_and_capped(root: Path) -> None:
    (root / "proc" / "cpuinfo").write_text(f"processor: 0\nmodel name: A\x1b[31m{'b' * 200}\n")
    model = basic.cpu(root)["model"]
    assert isinstance(model, str)
    assert len(model) == 80 and "\x1b" not in model


def test_cpu_without_a_model_line(root: Path) -> None:
    (root / "proc" / "cpuinfo").write_text("processor\t: 0\nBogoMIPS\t: 50.00\n")
    stats = basic.cpu(root)
    assert (stats["model"], stats["cores"]) == ("unknown", 1)


def test_memory_is_in_bytes(root: Path) -> None:
    assert basic.memory(root) == {
        "total_bytes": 32780000 * 1024,
        "available_bytes": 20000000 * 1024,
        "swap_total_bytes": 8000000 * 1024,
        "swap_free_bytes": 7999000 * 1024,
    }


def test_memory_without_swap_lines(root: Path) -> None:
    (root / "proc" / "meminfo").write_text("MemTotal: 10 kB\nMemAvailable: 4 kB\n")
    assert basic.memory(root) == {
        "total_bytes": 10240,
        "available_bytes": 4096,
        "swap_total_bytes": 0,
        "swap_free_bytes": 0,
    }


def test_disk_reports_root_and_home_only_by_label() -> None:
    asked: list[str] = []

    def statvfs(path: str) -> os.statvfs_result:
        asked.append(path)
        return fake_statvfs(path)

    stats = basic.disk(Path("/home/someone"), statvfs=statvfs)
    assert stats == {
        "/": {"size_bytes": 1000 * 4096, "free_bytes": 400 * 4096},
        "home": {"size_bytes": 9000 * 4096, "free_bytes": 2500 * 4096},
    }
    assert asked == ["/", "/home/someone"]
    assert "someone" not in json.dumps(stats)


def test_uptime(root: Path) -> None:
    assert basic.uptime(root) == {"seconds": 93784}


def test_gpu_is_not_available_yet() -> None:
    assert basic.gpu() == {"available": False, "reason": "arrives in a later phase"}


def test_system_stats_dispatches_every_topic(root: Path) -> None:
    home = Path("/home/someone")
    assert basic.system_stats("cpu", root=root) == basic.cpu(root)
    assert basic.system_stats("memory", root=root) == basic.memory(root)
    assert basic.system_stats("uptime", root=root) == basic.uptime(root)
    assert basic.system_stats("gpu", root=root) == basic.gpu()
    assert basic.system_stats("disk", home=home, statvfs=fake_statvfs) == basic.disk(
        home, statvfs=fake_statvfs
    )


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("loadavg", "garbage\n"),
        ("loadavg", "0.1 0.2\n"),
        ("meminfo", "MemTotal: lots\n"),
        ("uptime", ""),
        ("uptime", "soon\n"),
        ("uptime", "inf 1.0\n"),
        ("uptime", "nan 1.0\n"),
        ("loadavg", "0.1 nan 0.3 1/2 3\n"),
        ("loadavg", "inf 0.2 0.3 1/2 3\n"),
    ],
)
def test_unreadable_numbers_are_reported_not_raised(root: Path, name: str, content: str) -> None:
    (root / "proc" / name).write_text(content)
    topic = {"loadavg": "cpu", "meminfo": "memory", "uptime": "uptime"}[name]
    unavailable = {"available": False, "reason": "could not be read"}
    assert basic.system_stats(topic, root=root) == unavailable  # type: ignore[arg-type]


def test_an_unknown_topic_is_not_answered_as_another_topic(root: Path) -> None:
    answer = basic.system_stats("processes", root=root)  # type: ignore[arg-type]
    assert answer == {"available": False, "reason": "unknown topic"}


def test_a_missing_proc_is_reported_not_raised(tmp_path: Path) -> None:
    def no_statvfs(path: str) -> os.statvfs_result:
        raise FileNotFoundError(path)

    unavailable = {"available": False, "reason": "could not be read"}
    for topic in ("cpu", "memory", "uptime"):
        assert basic.system_stats(topic, root=tmp_path) == unavailable  # type: ignore[arg-type]
    assert basic.system_stats("disk", statvfs=no_statvfs) == unavailable


def test_the_topics_match_the_tool_contract() -> None:
    from waddle_core.tools import SystemStatsArgs

    contract = get_args(SystemStatsArgs.model_fields["topic"].annotation)
    assert set(get_args(basic.Topic)) == set(contract)


def _flat(value: object) -> list[object]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _flat(item)]
    return [value]


def test_smoke_against_the_real_machine() -> None:
    for topic in get_args(basic.Topic):
        stats = basic.system_stats(topic)
        encoded = json.dumps(stats)
        assert len(encoded) < 400
        assert str(Path.home()) not in encoded
        for leaf in _flat(stats):
            assert isinstance(leaf, bool | int | float | str)
            assert not isinstance(leaf, str) or len(leaf) <= 80
    cpu = basic.system_stats("cpu")
    assert isinstance(cpu["cores"], int) and cpu["cores"] >= 1
    memory = basic.system_stats("memory")
    assert isinstance(memory["total_bytes"], int) and isinstance(memory["available_bytes"], int)
    assert memory["total_bytes"] >= memory["available_bytes"] > 0
    disk = basic.system_stats("disk")
    assert set(disk) == {"/", "home"}
    uptime = basic.system_stats("uptime")
    assert isinstance(uptime["seconds"], int) and uptime["seconds"] > 0
