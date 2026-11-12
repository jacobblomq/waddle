"""App identity, steps 1-2 of the version waterfall (ARCHITECTURE §4.6).

Everything runs against small fixture trees: ``fixtures/proc/procfs`` stands in for /proc
and ``fixtures/proc/rootfs`` for /. The home directory inside the root is /home/tux.
Process ids used by the fixtures are listed next to the tests that use them.
"""

from __future__ import annotations

import dataclasses
import itertools
import os
import random
import shutil
import sqlite3
import stat
import statistics
import sys
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

from waddle_desktop import log
from waddle_desktop.identity.cache import (
    UNKNOWN_IDENTITY,
    AppIdentity,
    IdentityCache,
    cache_key,
    identify,
)
from waddle_desktop.identity.packaging import (
    DPKG_MAX_LISTS,
    UNKNOWN_PACKAGE,
    PackageInfo,
    Packaging,
    RealFs,
    detect_packaging,
)
from waddle_desktop.identity.resolve import ResolvedBinary, identity_name, resolve_binary
from waddle_desktop.identity.snapshot import (
    ENV_ALLOWLIST,
    MAX_ARG_BYTES,
    MAX_ARGS,
    MAX_CGROUP_BYTES,
    MAX_FLATPAK_INFO_BYTES,
    ProcessSnapshot,
    ProcReader,
)
from waddle_desktop.identity.version import (
    Channel,
    Version,
    detect_channel,
    detect_edition,
    docs_key,
    parse_version,
    version_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "proc"
PROC = FIXTURES / "procfs"
ROOT = FIXTURES / "rootfs"
HOME = Path("/home/tux")
SECRET = "hunter2-TOPSECRET"
KDENLIVE_COMMIT = "5c1f6d0e9a3b47c2a8e1d4f7b6a59382716e0c4d3b2a19f8e7d6c5b4a3928170"
KDENLIVE_DEPLOY = f"var/lib/flatpak/app/org.kde.kdenlive/x86_64/stable/{KDENLIVE_COMMIT}"
ALL_PIDS = [*sorted(int(p.name) for p in PROC.iterdir()), 2000, 99999]


class CountingFs:
    """The real reader over a fixture root, counting every call that reaches it."""

    def __init__(self, root: Path = ROOT) -> None:
        self._fs = RealFs(root)
        self.calls: Counter[str] = Counter()
        self.paths: list[str] = []

    def _note(self, method: str, path: str) -> None:
        self.calls[method] += 1
        self.paths.append(path)

    def read_text(self, path: str, cap: int) -> str | None:
        self._note("read_text", path)
        return self._fs.read_text(path, cap)

    def iter_lines(self, path: str, cap: int) -> Iterator[str]:
        self._note("iter_lines", path)
        return self._fs.iter_lines(path, cap)

    def exists(self, path: str) -> bool:
        self._note("exists", path)
        return self._fs.exists(path)

    def listdir(self, path: str) -> list[str]:
        self._note("listdir", path)
        return self._fs.listdir(path)

    def stat(self, path: str) -> tuple[int, int] | None:
        self._note("stat", path)
        return self._fs.stat(path)


class ExplodingFs:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError("boom")


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """A private, writable copy of the fixtures: (proc root, filesystem root)."""
    shutil.copytree(FIXTURES, tmp_path / "t", symlinks=True)
    return tmp_path / "t" / "procfs", tmp_path / "t" / "rootfs"


def snapshot_of(pid: int = 4242, **fields: object) -> ProcessSnapshot:
    base: dict[str, object] = {
        "pid": pid,
        "ppid": 800,
        "exe": None,
        "cmdline": (),
        "env": {},
        "cgroup": "",
        "root_flatpak_info": None,
        "exe_stat": None,
    }
    return ProcessSnapshot(**{**base, **fields})  # type: ignore[arg-type]


def resolved(pid: int, proc: Path = PROC) -> ResolvedBinary:
    return resolve_binary(ProcReader(proc).chain(pid))


def packaging_of(pid: int, proc: Path = PROC, root: Path = ROOT) -> PackageInfo:
    chain = ProcReader(proc).chain(pid)
    return detect_packaging(resolve_binary(chain), chain, RealFs(root), home=HOME)


def identify_fixture(pid: int, **kwargs: str) -> AppIdentity:
    return identify(pid, ProcReader(PROC), RealFs(ROOT), HOME, None, **kwargs)


# --------------------------------------------------------------------------- snapshot.py


def test_snapshot_reads_the_documented_fields() -> None:
    snap = ProcReader(PROC).read(1000)
    assert snap is not None
    assert (snap.pid, snap.ppid, snap.exe, snap.cmdline) == (
        1000,
        800,
        "/usr/bin/blender",
        ("blender",),
    )
    assert snap.cgroup.startswith("0::/user.slice/")
    assert snap.root_flatpak_info is None
    assert dict(snap.env) == {
        "GIO_LAUNCHED_DESKTOP_FILE": "/usr/share/applications/blender.desktop"
    }


def test_only_allowlisted_environment_survives_and_secrets_never_do() -> None:
    reader = ProcReader(PROC)
    # The fixture environ really contains the secret, so this test can fail.
    assert SECRET.encode() in (PROC / "1300" / "environ").read_bytes()
    for pid in ALL_PIDS:
        snap = reader.read(pid)
        if snap is None:
            continue
        assert set(snap.env) <= ENV_ALLOWLIST
        assert SECRET not in repr(snap)
        assert "AWS_SECRET" not in repr(snap)
        assert "SNAPPY" not in snap.env  # a prefix of an allowlisted name is not enough
    firefox = reader.read(1300)
    assert firefox is not None
    assert dict(firefox.env) == {
        "SNAP": "/snap/firefox/5187",
        "SNAP_NAME": "firefox",
        "SNAP_INSTANCE_NAME": "firefox",
        "SNAP_VERSION": "145.0.1-1",
        "SNAP_REVISION": "5187",
    }
    with pytest.raises(TypeError):
        firefox.env["HOME"] = "/root"  # type: ignore[index]


def test_environment_parsing_survives_odd_and_oversized_entries(tree: tuple[Path, Path]) -> None:
    proc, _ = tree
    entries = [
        b"NOEQUALS",
        b"=empty-key",
        b"SNAP_NAME=first",
        b"SNAP_NAME=second",
        b"APPIMAGE=" + b"x" * 5000,  # over the per-value cap: dropped
        b"FLATPAK_ID=\xff\xfe.bad",
        b"container=flatpak",
    ]
    (proc / "1000" / "environ").write_bytes(b"\0".join(entries))  # no trailing NUL
    snap = ProcReader(proc).read(1000)
    assert snap is not None
    assert dict(snap.env) == {
        "SNAP_NAME": "first",
        "FLATPAK_ID": "\ufffd\ufffd.bad",
        "container": "flatpak",
    }


def test_vanished_and_invalid_pids_give_none() -> None:
    reader = ProcReader(PROC)
    assert reader.read(2000) is None  # no such directory
    assert reader.read(99999) is None
    for bad in (0, -1, True, 2**40, "1000", None, 1.5):
        assert reader.read(bad) is None  # type: ignore[arg-type]
    assert ProcReader(Path("/nonexistent/proc")).read(1) is None


def test_ppid_is_parsed_after_the_last_parenthesis() -> None:
    raw = (PROC / "2100" / "stat").read_text()
    assert raw.startswith("2100 (Web Content) R 1 (x) S 800 ")
    assert raw.split()[3] != "800"  # what naive whitespace splitting would have returned
    snap = ProcReader(PROC).read(2100)
    assert snap is not None and snap.ppid == 800


@pytest.mark.parametrize("content", ["", "1000", "1000 (blender", "1000 (x) S", "1000 (x) S abc 1"])
def test_unparseable_stat_means_no_snapshot(tree: tuple[Path, Path], content: str) -> None:
    proc, _ = tree
    (proc / "1000" / "stat").write_text(content)
    assert ProcReader(proc).read(1000) is None


def test_deleted_marker_is_stripped_from_exe() -> None:
    assert str((PROC / "2200" / "exe").readlink()) == "/usr/bin/blender (deleted)"
    snap = ProcReader(PROC).read(2200)
    assert snap is not None and snap.exe == "/usr/bin/blender"


def test_a_process_with_only_stat_readable_still_yields_a_snapshot() -> None:
    snap = ProcReader(PROC).read(2800)
    assert snap == snapshot_of(2800, ppid=2, env=snap.env if snap else {})
    assert snap is not None and dict(snap.env) == {}


def test_flatpak_info_is_read_from_the_process_root() -> None:
    snap = ProcReader(PROC).read(1202)
    assert snap is not None and snap.root_flatpak_info is not None
    assert "name=org.kde.kdenlive" in snap.root_flatpak_info
    assert snap.env["FLATPAK_ID"] == "org.kde.kdenlive"


def test_reads_are_capped(tree: tuple[Path, Path]) -> None:
    proc, _ = tree
    args = [b"a" * (MAX_ARG_BYTES + 500)] + [b"arg%d" % i for i in range(MAX_ARGS + 40)]
    (proc / "1202" / "cmdline").write_bytes(b"\0".join(args) + b"\0")
    (proc / "1202" / "cgroup").write_bytes(b"c" * (MAX_CGROUP_BYTES * 3))
    (proc / "1202" / "root" / ".flatpak-info").write_bytes(b"i" * (MAX_FLATPAK_INFO_BYTES * 3))
    snap = ProcReader(proc).read(1202)
    assert snap is not None
    assert len(snap.cmdline) == MAX_ARGS
    assert len(snap.cmdline[0]) == MAX_ARG_BYTES
    assert snap.cmdline[1] == "arg0"
    assert len(snap.cgroup) == MAX_CGROUP_BYTES
    assert snap.root_flatpak_info is not None
    assert len(snap.root_flatpak_info) == MAX_FLATPAK_INFO_BYTES


def test_exe_stat_follows_the_link(tmp_path: Path) -> None:
    target = tmp_path / "bin" / "tool"
    target.parent.mkdir()
    target.write_bytes(b"x" * 321)
    proc_dir = tmp_path / "proc" / "77"
    proc_dir.mkdir(parents=True)
    (proc_dir / "stat").write_text("77 (tool) S 1 77 77 0\n")
    (proc_dir / "exe").symlink_to(target)
    snap = ProcReader(tmp_path / "proc").read(77)
    assert snap is not None
    assert snap.exe == str(target)
    assert snap.exe_stat == (321, target.stat().st_mtime_ns)


def test_chain_walks_up_and_stops_before_pid_1() -> None:
    reader = ProcReader(PROC)
    assert [s.pid for s in reader.chain(1502)] == [1502, 1501, 1500, 800]
    assert [s.pid for s in reader.chain(1502, max_depth=2)] == [1502, 1501]
    assert [s.pid for s in reader.chain(1)] == [1]


def test_chain_survives_loops_and_vanished_parents() -> None:
    reader = ProcReader(PROC)
    assert [s.pid for s in reader.chain(2700)] == [2700, 2701]
    assert [s.pid for s in reader.chain(2001)] == [2001]  # parent 2000 is gone
    assert reader.chain(2000) == []


def test_reading_the_real_proc_of_this_process() -> None:
    snap = ProcReader().read(os.getpid())
    assert snap is not None
    assert snap.ppid == os.getppid()
    assert snap.exe == os.path.realpath(sys.executable)
    assert snap.exe_stat is not None and snap.exe_stat[0] == Path(snap.exe).stat().st_size
    assert snap.cmdline and len(snap.cmdline) <= MAX_ARGS
    assert set(snap.env) <= ENV_ALLOWLIST
    assert not (set(os.environ) - ENV_ALLOWLIST) & set(snap.env)


# --------------------------------------------------------------------------- resolve.py

RESOLVE_CASES = [
    # pid, exe, wrapper kinds, script, identity name
    (1000, "/usr/bin/blender", (), None, "blender"),
    (1100, "/home/tux/apps/blender-4.5.3-linux-x64/blender", (), None, "blender"),
    (1202, "/app/bin/kdenlive", ("flatpak",), None, "kdenlive"),
    (1201, "/usr/bin/bwrap", ("flatpak", "bwrap"), None, "bwrap"),
    (1200, "/usr/bin/bwrap", ("bwrap",), None, "bwrap"),
    (1300, "/snap/firefox/5187/usr/lib/firefox/firefox", ("snap",), None, "firefox"),
    (1400, "/home/tux/Applications/krita-5.2.11-x86_64.AppImage", ("appimage",), None, "krita"),
    (1500, "/opt/Obsidian/obsidian", (), None, "obsidian"),
    (1501, "/opt/Obsidian/obsidian", ("helper",), None, "obsidian"),
    (1502, "/opt/Obsidian/obsidian", ("helper",), None, "obsidian"),
    (
        1600,
        "/usr/lib/electron37/electron",
        ("electron",),
        "/usr/lib/element/resources/app.asar",
        "element",
    ),
    (
        # the renderer's own command line no longer names the app: only its parent's does
        1601,
        "/usr/lib/electron37/electron",
        ("helper", "electron"),
        "/usr/lib/element/resources/app.asar",
        "element",
    ),
    (1700, "/usr/bin/python3.14", ("python",), "/usr/bin/virt-manager", "virt-manager"),
    (1800, "/usr/bin/dash", ("shell",), "/opt/pycharm-2025.2/bin/pycharm.sh", "pycharm"),
    (1900, "/opt/resolve/bin/resolve", (), None, "resolve"),
    (2200, "/usr/bin/blender", (), None, "blender"),
    (
        2300,
        "/nix/store/1b9p07z77phvv2hf6gm9f28syp39f1ag-gimp-3.0.4/bin/.gimp-wrapped",
        ("wrapped",),
        None,
        "gimp",
    ),
    (2310, "/usr/lib/thunderbird/thunderbird.bin", ("wrapped",), None, "thunderbird"),
    (
        2400,
        "/usr/lib/jvm/java-21-openjdk-amd64/bin/java",
        ("java",),
        "/opt/jdownloader/JDownloader.jar",
        "jdownloader",
    ),
    (2500, "/usr/bin/bash", (), None, "bash"),  # an interactive shell wraps nothing
    (2800, "", (), None, ""),
]


@pytest.mark.parametrize(("pid", "exe", "kinds", "script", "name"), RESOLVE_CASES)
def test_resolve_binary_from_fixtures(
    pid: int, exe: str, kinds: tuple[str, ...], script: str | None, name: str
) -> None:
    binary = resolved(pid)
    assert (binary.exe, binary.wrapper_kinds, binary.script) == (exe, kinds, script)
    assert binary.identity_path == (script or exe)
    assert identity_name(binary) == name


def test_resolve_never_looks_beyond_the_chain_it_is_given() -> None:
    chain = ProcReader(PROC).chain(1502)
    # Only the renderer and the zygote: the real app is out of reach, so the last helper
    # has to do, and it is still marked as one.
    binary = resolve_binary(chain[:2])
    assert binary.wrapper_kinds == ("helper",)
    assert binary.argv0 == "/opt/Obsidian/obsidian"
    assert resolve_binary([]) == ResolvedBinary("", "", (), None)


def test_helper_walk_picks_the_nearest_non_helper_not_the_root() -> None:
    chain = [
        snapshot_of(4, exe="/opt/app/helper", cmdline=("/opt/app/helper", "--type=utility")),
        snapshot_of(3, exe="/opt/app/helper", cmdline=("helper", "--x", "--type=gpu-process")),
        snapshot_of(2, exe="/opt/app/app", cmdline=("/opt/app/app", "--flag")),
        snapshot_of(1, exe="/usr/bin/konsole", cmdline=("konsole",)),
    ]
    assert resolve_binary(chain) == ResolvedBinary(
        "/opt/app/app", "/opt/app/app", ("helper",), None
    )
    # argv[0] itself is never taken for a --type= flag
    odd = [snapshot_of(1, exe="/usr/bin/x", cmdline=("--type=renderer",))]
    assert resolve_binary(odd).wrapper_kinds == ()


SCRIPT_CASES = [
    ("/usr/bin/python3", ("python3", "/opt/app/main.py", "--x"), "/opt/app/main.py", "python"),
    ("/usr/bin/python3.12", ("python3", "-X", "dev", "-W", "ignore", "run.py"), "run.py", "python"),
    ("/usr/bin/python3", ("python3", "-m", "http.server"), None, None),
    ("/usr/bin/python3", ("python3", "-c", "import x; x.main('/a/b.py')"), None, None),
    ("/usr/bin/python3", ("python3",), None, None),
    ("/usr/bin/python3", ("python3", "-"), None, None),
    ("/usr/bin/python3", ("python3", "--", "/opt/s.py"), "/opt/s.py", "python"),
    (
        "/usr/bin/bash",
        ("bash", "-e", "-o", "pipefail", "/usr/bin/launch"),
        "/usr/bin/launch",
        "shell",
    ),
    ("/usr/bin/bash", ("bash", "-c", "/usr/bin/foo --bar"), None, None),
    ("/usr/bin/bash", ("bash", "-ec", "/usr/bin/foo"), None, None),
    ("/usr/bin/bash", ("/bin/bash", "--login"), None, None),
    ("/usr/bin/dash", ("sh", "wordwithoutslash"), None, None),
    ("/usr/bin/zsh", ("zsh", "/home/tux/bin/start.zsh"), "/home/tux/bin/start.zsh", "shell"),
    ("/usr/bin/perl", ("perl", "-w", "/usr/bin/exiftool", "a.jpg"), "/usr/bin/exiftool", "perl"),
    ("/usr/bin/perl5.40.1", ("perl", "-e", "print 1"), None, None),
    (
        "/usr/bin/node",
        ("node", "--require", "/x/hook.js", "/opt/app/server.js"),
        "/opt/app/server.js",
        "node",
    ),
    ("/usr/bin/node", ("node", "-e", "require('/x.js')"), None, None),
    ("/usr/bin/java", ("java", "-cp", "/a.jar", "com.example.Main"), None, None),
    ("/usr/bin/java", ("java", "-jar"), None, None),
    ("/usr/bin/java", ("java", "-Dx=1", "-jar", "/opt/t/tool.jar"), "/opt/t/tool.jar", "java"),
    ("/usr/lib/electron/electron", ("electron", "/usr/lib/code"), "/usr/lib/code", "electron"),
    ("/usr/bin/python3", ("python3", "/tmp/\x1b[31mevil.py"), None, None),
    ("/usr/bin/python3", ("python3", "/" + "a" * 5000), None, None),
    ("/usr/bin/pythonista", ("pythonista", "/opt/x.py"), None, None),  # not an interpreter
]


@pytest.mark.parametrize(("exe", "cmdline", "script", "kind"), SCRIPT_CASES)
def test_interpreter_script_detection(
    exe: str, cmdline: tuple[str, ...], script: str | None, kind: str | None
) -> None:
    binary = resolve_binary([snapshot_of(exe=exe, cmdline=cmdline)])
    assert binary.script == script
    assert binary.wrapper_kinds == ((kind,) if kind else ())
    assert binary.exe == exe


def test_appimage_identity_comes_from_the_environment_of_the_chain() -> None:
    child = snapshot_of(2, exe="/tmp/.mount_MyAppXyZ/usr/bin/python3", cmdline=("python3", "/x.py"))
    parent = snapshot_of(
        1, exe="/tmp/.mount_MyAppXyZ/AppRun", env={"APPIMAGE": "/home/tux/MyApp-2.0.AppImage"}
    )
    binary = resolve_binary([child, parent])
    assert binary == ResolvedBinary("/home/tux/MyApp-2.0.AppImage", "python3", ("appimage",), None)
    assert identity_name(binary) == "myapp"
    # Without APPIMAGE anywhere the mount path is all there is; a relative value is ignored.
    alone = resolve_binary([dataclasses.replace(child, env={"APPIMAGE": "relative.AppImage"})])
    assert alone.exe == "/tmp/.mount_MyAppXyZ/usr/bin/python3"
    assert alone.wrapper_kinds == ("appimage",)
    # APPDIR marks an extracted or oddly mounted image
    custom = snapshot_of(
        exe="/opt/img/usr/bin/app", env={"APPDIR": "/opt/img", "APPIMAGE": "/data/App.AppImage"}
    )
    assert resolve_binary([custom]).exe == "/data/App.AppImage"
    outside = dataclasses.replace(custom, exe="/usr/bin/xdg-open")
    assert resolve_binary([outside]).exe == "/usr/bin/xdg-open"


def test_snap_confine_and_argv0_fallback() -> None:
    confine = snapshot_of(
        exe="/usr/lib/snapd/snap-confine",
        cmdline=("/usr/lib/snapd/snap-confine", "snap.firefox.firefox", "/usr/lib/snapd/snap-exec"),
    )
    assert resolve_binary([confine]).wrapper_kinds == ("snap-confine",)
    unreadable = snapshot_of(exe=None, cmdline=("/usr/bin/python3", "/usr/bin/hplip-systray"))
    binary = resolve_binary([unreadable])
    assert (binary.exe, binary.script) == ("", "/usr/bin/hplip-systray")


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/usr/bin/gimp-3.0", "gimp"),
        ("/usr/bin/Blender", "blender"),
        ("/opt/x/DaVinci Resolve", "davinci-resolve"),
        ("/usr/bin/7zip", "7zip"),
        ("/usr/bin/qt6ct", "qt6ct"),
        ("/usr/bin/gtk4-demo", "gtk4-demo"),
        ("/usr/bin/firefox-esr", "firefox-esr"),
        ("/usr/bin/appimagetool-x86_64", "appimagetool"),
        ("/home/tux/Godot_v4.5.1-stable_linux.x86_64", "godot"),
        ("/usr/bin/x\x1b[31m;rm", "x-31m-rm"),
        ("/usr/bin/.bin", "bin"),
        ("", ""),
    ],
)
def test_identity_name_strips_versions_and_unsafe_characters(path: str, name: str) -> None:
    assert identity_name(ResolvedBinary(path, "", (), None)) == name


# --------------------------------------------------------------------------- version.py

VERSION_CASES = [
    ("2:4.5.3+dfsg-1ubuntu2", Version(4, 5, 3), "4.5.3"),
    ("4.5.3-0ubuntu1~26.04", Version(4, 5, 3), "4.5.3"),
    ("v21.1.2b", Version(21, 1, 2), "21.1.2"),
    ("25.08.3", Version(25, 8, 3), "25.08.3"),
    ("3.0.4-1.fc40", Version(3, 0, 4), "3.0.4"),
    ("5.3.0 Alpha", Version(5, 3, 0, "alpha"), "5.3.0-alpha"),
    ("5.3.0-alpha", Version(5, 3, 0, "alpha"), "5.3.0-alpha"),
    ("5.4 nightly", Version(5, 4, None, "nightly"), "5.4-nightly"),
    ("21.1.0.17", Version(21, 1, 0), "21.1.0.17"),
    ("4.5", Version(4, 5, None), "4.5"),
    ("4.5.3", Version(4, 5, 3), "4.5.3"),
    ("  4.5.3\n", Version(4, 5, 3), "4.5.3"),
    ("Blender 4.5.3 LTS", Version(4, 5, 3), "4.5.3"),
    ("blender-4.5.3-linux-x64", Version(4, 5, 3), "4.5.3"),
    ("krita-5.2.11-x86_64", Version(5, 2, 11), "5.2.11"),
    ("DaVinci_Resolve_Studio_21.1.2_Linux", Version(21, 1, 2), "21.1.2"),
    (
        "blender-5.4.0-alpha+main.a1b2c3d4e5f6-linux.x86_64-release",
        Version(5, 4, 0, "alpha"),
        "5.4.0-alpha",
    ),
    ("1:5.1.0-1ubuntu1", Version(5, 1, 0), "5.1.0"),
    ("145.0.1-1", Version(145, 0, 1), "145.0.1"),
    ("142.0.7444.59", Version(142, 0, 7444), "142.0.7444.59"),
    ("2025.2.4", Version(2025, 2, 4), "2025.2.4"),
    ("3.13.0a2", Version(3, 13, 0, "alpha"), "3.13.0-alpha"),
    ("21.0b3", Version(21, 0, None, "beta"), "21.0-beta"),
    ("7.0~rc1-1", Version(7, 0, None, "rc"), "7.0-rc"),
    ("1.2.3-beta.2", Version(1, 2, 3, "beta"), "1.2.3-beta"),
    ("1.2.3-preview", Version(1, 2, 3, "beta"), "1.2.3-beta"),
    ("1.0.0-dev", Version(1, 0, 0, "nightly"), "1.0.0-nightly"),
    ("2.10.38-1+b1", Version(2, 10, 38), "2.10.38"),
    ("1.0.2u", Version(1, 0, 2), "1.0.2"),
    ("5.2.37-2ubuntu1 (a1b2c3d4)", Version(5, 2, 37), "5.2.37"),
    ("6.6.0 (git 9f8e7d6c5b)", Version(6, 6, 0), "6.6.0"),
    ("V2.0", Version(2, 0, None), "2.0"),
    ("132", Version(132, 0, None), "132"),
    ("v7 beta", Version(7, 0, None, "beta"), "7-beta"),
    ("0.0.1", Version(0, 0, 1), "0.0.1"),
    ("100000.0", Version(100000, 0, None), "100000.0"),
    ("1.2.3-src", Version(1, 2, 3), "1.2.3"),  # "rc" inside a word is not a tag
    ("1.2.3-preinst", Version(1, 2, 3), "1.2.3"),
]


@pytest.mark.parametrize(("text", "expected", "raw"), VERSION_CASES)
def test_parse_version(text: str, expected: Version, raw: str) -> None:
    parsed = parse_version(text)
    assert parsed is not None
    # == treats a missing patch as 0, so the written fields are compared one by one.
    assert (parsed.major, parsed.minor, parsed.patch, parsed.pre) == (
        expected.major,
        expected.minor,
        expected.patch,
        expected.pre,
    )
    assert version_text(text) == raw
    again = parse_version(raw)
    assert again is not None and repr(again) == repr(parsed)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "abc",
        "deadbeef",
        "a1b2c3d",
        "1a2b3c4d",
        "x86_64",
        "linux-x64",
        "python3.12",
        "v",
        "....",
        "1.",
        "1234567",
        "20240101",
        "999999999.1",
        "1.200000",
        "100001.0",
        "7 apples",
        "build 7",
        None,
        42,
        b"4.5.3",
    ],
)
def test_parse_version_rejects_non_versions(text: object) -> None:
    assert parse_version(text) is None  # type: ignore[arg-type]
    assert version_text(text) is None  # type: ignore[arg-type]


def test_version_ordering_and_docs_key() -> None:
    ordered = [
        Version(4, 5, None, "nightly"),
        Version(4, 5, 0, "alpha"),
        Version(4, 5, 0, "beta"),
        Version(4, 5, 0, "rc"),
        Version(4, 5, 0),
        Version(4, 5, 3),
        Version(4, 10, 0, "alpha"),
        Version(5, 0, None),
    ]
    shuffled = list(ordered)
    random.Random(3).shuffle(shuffled)
    assert shuffled != ordered and sorted(shuffled) == ordered
    assert all(a < b for a, b in itertools.pairwise(ordered))
    assert Version(4, 5, None) == Version(4, 5, 0)
    assert hash(Version(4, 5, None)) == hash(Version(4, 5, 0))
    assert Version(4, 5, 0) != Version(4, 5, 0, "rc")
    assert docs_key(Version(4, 5, 3, "alpha")) == "4.5"
    assert docs_key(Version(25, 8, None)) == "25.8"
    assert str(Version(5, 4, None, "nightly")) == "5.4-nightly"
    for bad in ((-1, 0, 0, ""), (1, 0, 0, "gamma"), (1, 2**20, 0, ""), (True, 0, 0, "")):
        with pytest.raises(ValueError, match=r"version|pre-release"):
            Version(*bad)


@pytest.mark.parametrize(
    ("raw", "channel"),
    [
        ("4.5.3", Channel.STABLE),
        ("2:4.5.3+dfsg-1ubuntu2", Channel.STABLE),
        ("4.2.0", Channel.STABLE),  # an LTS series, but nothing here says so
        ("Blender 4.5.3 LTS", Channel.LTS),
        ("5.3.0 Alpha", Channel.ALPHA),
        ("5.4 nightly", Channel.NIGHTLY),
        ("21.0b3", Channel.BETA),
        ("7.0~rc1-1", Channel.RC),
        ("Nightly 5.0-beta", Channel.NIGHTLY),
        ("5.0 Beta LTS", Channel.BETA),
        ("results-1.0", Channel.STABLE),
        ("", Channel.STABLE),
    ],
)
def test_detect_channel(raw: str, channel: Channel) -> None:
    assert detect_channel(raw) is channel


@pytest.mark.parametrize(
    ("app_id", "hints", "edition"),
    [
        ("resolve", ["DaVinci_Resolve_Studio_21.1.2_Linux.run"], "Studio"),
        ("com.blackmagicdesign.resolve", ["opt", "DaVinci Resolve Studio"], "Studio"),
        ("resolve", ["DaVinci_Resolve_21.1.2_Linux.run"], None),  # never guessed to be "Free"
        ("resolve", [], None),
        ("resolve", ["recording-studios"], None),
        ("blender", ["Blender Studio"], None),  # no editions known for this app
        ("pycharm", ["pycharm-community-2025.2"], "Community"),
        ("pycharm", ["pycharm-professional"], "Professional"),
        ("resolve", [None, 7, "studio"], "Studio"),
    ],
)
def test_detect_edition(app_id: str, hints: list[str], edition: str | None) -> None:
    assert detect_edition(app_id, hints) == edition


def test_detect_edition_only_looks_at_a_bounded_number_of_hints() -> None:
    assert detect_edition("resolve", ["x"] * 31 + ["studio"]) == "Studio"
    assert detect_edition("resolve", ["x"] * 32 + ["studio"]) is None


# --------------------------------------------------------------------------- packaging.py


@pytest.mark.parametrize(
    ("pid", "expected"),
    [
        (1000, PackageInfo(Packaging.DEB, "blender", "2:4.5.3+dfsg-1ubuntu2", None, "dpkg")),
        (2200, PackageInfo(Packaging.DEB, "blender", "2:4.5.3+dfsg-1ubuntu2", None, "dpkg")),
        (
            1100,
            PackageInfo(Packaging.OPT, "blender-4.5.3-linux-x64", "4.5.3", None, "dir-name"),
        ),
        (
            1202,
            PackageInfo(
                Packaging.FLATPAK, "org.kde.kdenlive", "25.08.3", "stable", "flatpak-metadata"
            ),
        ),
        (1300, PackageInfo(Packaging.SNAP, "firefox", "145.0.1-1", None, "snap-env")),
        (1310, PackageInfo(Packaging.SNAP, "chromium", "142.0.7444.59", None, "snap-yaml")),
        (
            1400,
            PackageInfo(
                Packaging.APPIMAGE, "krita-5.2.11-x86_64.AppImage", "5.2.11", None, "appimage-name"
            ),
        ),
        (1502, PackageInfo(Packaging.DEB, "obsidian", "1.10.3", None, "dpkg")),
        (1600, PackageInfo(Packaging.SYSTEM, "app.asar", None, None, "unknown")),
        (1700, PackageInfo(Packaging.DEB, "virt-manager", "1:5.1.0-1ubuntu1", None, "dpkg")),
        (1800, PackageInfo(Packaging.OPT, "pycharm-2025.2", "2025.2.4", None, "version-file")),
        (1900, PackageInfo(Packaging.OPT, "resolve", None, None, "unknown")),
        (2300, UNKNOWN_PACKAGE),
        (2500, PackageInfo(Packaging.DEB, "bash", "5.3-1ubuntu1", None, "dpkg")),  # usrmerge
        (2600, PackageInfo(Packaging.SYSTEM, "mytool", None, None, "unknown")),
        (2610, PackageInfo(Packaging.SYSTEM, "handbuilt", None, None, "unknown")),
        (2800, UNKNOWN_PACKAGE),
    ],
)
def test_detect_packaging_from_fixtures(pid: int, expected: PackageInfo) -> None:
    assert packaging_of(pid) == expected


def test_usrmerge_alias_works_in_both_directions(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    # The fixture lists /bin/bash while the process runs /usr/bin/bash ...
    assert "/usr/bin/bash" not in (root / "var/lib/dpkg/info/bash.list").read_text()
    assert packaging_of(2500, proc, root).package_id == "bash"
    # ... and the other way round.
    (root / "var/lib/dpkg/info/bash.list").write_text("/.\n/usr/bin/bash\n")
    (proc / "2500" / "exe").unlink()
    (proc / "2500" / "exe").symlink_to("/bin/bash")
    assert packaging_of(2500, proc, root).package_id == "bash"
    (root / "var/lib/dpkg/info/bash.list").write_text("/.\n/usr/sbin/bash\n")
    assert packaging_of(2500, proc, root).kind is Packaging.SYSTEM


def test_dpkg_ownership_needs_an_exact_newline_terminated_line(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    info = root / "var/lib/dpkg/info"
    decoy = (info / "decoy.list").read_text()
    assert "/usr/bin/blender-launcher\n" in decoy and decoy.endswith("/usr/bin/blender")
    (info / "blender.list").unlink()
    assert packaging_of(1000, proc, root).kind is Packaging.SYSTEM
    (info / "decoy.list").write_text(decoy + "\n")
    assert packaging_of(1000, proc, root).package_id == "decoy"


def test_dpkg_version_comes_from_the_installed_stanza_of_that_package(
    tree: tuple[Path, Path],
) -> None:
    proc, root = tree
    status = root / "var/lib/dpkg/status"
    text = status.read_text()
    # The fixture has a removed i386 "blender" stanza and a "blender-data" stanza before
    # the installed one, and a " Version: 9.9.9" continuation line inside each description.
    assert text.index("Version: 4.2.0-1") < text.index("Version: 2:4.5.3+dfsg-1ubuntu2")
    assert packaging_of(1000, proc, root).version == "2:4.5.3+dfsg-1ubuntu2"
    status.write_text(text.replace("Version: 2:4.5.3+dfsg-1ubuntu2\n", "", 1))
    assert packaging_of(1000, proc, root) == PackageInfo(
        Packaging.DEB, "blender", None, None, "dpkg"
    )
    status.write_text(text.rstrip("\n"))  # last stanza without the closing blank line
    assert packaging_of(1700, proc, root).version == "1:5.1.0-1ubuntu1"
    status.unlink()
    assert packaging_of(1000, proc, root).version is None


def test_dpkg_respects_the_architecture_of_a_multiarch_list(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    info = root / "var/lib/dpkg/info"
    (info / "blender.list").rename(info / "blender:i386.list")
    # Only the removed 4.2.0 stanza is i386, and it is not installed.
    assert packaging_of(1000, proc, root) == PackageInfo(
        Packaging.DEB, "blender", None, None, "dpkg"
    )
    (info / "blender:i386.list").rename(info / "blender:amd64.list")
    assert packaging_of(1000, proc, root).version == "2:4.5.3+dfsg-1ubuntu2"


def test_dpkg_scan_is_capped_and_skipped_where_it_cannot_apply(tmp_path: Path) -> None:
    info = tmp_path / "var/lib/dpkg/info"
    info.mkdir(parents=True)
    for index in range(DPKG_MAX_LISTS + 5):
        (info / f"pkg{index:05d}.list").write_text("/usr/bin/other\n")
    (info / "zzz-last.list").write_text("/usr/bin/tool\n")
    fs = CountingFs(tmp_path)
    binary = ResolvedBinary("/usr/bin/tool", "tool", (), None)
    assert detect_packaging(binary, [], fs, home=HOME).kind is Packaging.SYSTEM
    assert fs.calls["read_text"] == DPKG_MAX_LISTS
    # A name match is tried first, so the common case does not depend on the cap.
    (info / "zzz-last.list").rename(info / "tool.list")
    fs = CountingFs(tmp_path)
    assert detect_packaging(binary, [], fs, home=HOME).package_id == "tool"
    assert fs.calls["read_text"] == 1
    for path in ("/usr/local/bin/tool", "/home/tux/bin/tool", "/tmp/tool", "relative/tool", ""):
        fs = CountingFs(tmp_path)
        detect_packaging(ResolvedBinary(path, "tool", (), None), [], fs, home=HOME)
        assert fs.calls["listdir"] == 0, path


def test_flatpak_falls_back_to_current_active_and_the_user_installation(
    tree: tuple[Path, Path],
) -> None:
    proc, root = tree
    info_file = proc / "1202" / "root" / ".flatpak-info"
    info = info_file.read_text()
    deploy = root / KDENLIVE_DEPLOY

    # app-path outside every known installation is not believed (nor followed).
    evil = root / "home/tux/evil/app/org.kde.kdenlive/files"
    shutil.copytree(deploy, evil.parent, dirs_exist_ok=True)
    xml = evil.parent / "export/share/metainfo/org.kde.kdenlive.metainfo.xml"
    xml.write_text(xml.read_text().replace("25.08.3", "66.6.6"))
    info_file.write_text(
        info.replace("app-path=/var/lib/flatpak", "app-path=/home/tux/evil/x/../..")
    )
    assert packaging_of(1202, proc, root) == PackageInfo(
        Packaging.FLATPAK, "org.kde.kdenlive", None, "stable", "flatpak-info"
    )
    info_file.write_text(
        info.replace(
            f"app-path=/var/lib/flatpak/app/org.kde.kdenlive/x86_64/stable/{KDENLIVE_COMMIT}",
            "app-path=/home/tux/evil/app/org.kde.kdenlive",
        )
    )
    assert packaging_of(1202, proc, root).version is None

    # No app-path at all: <installation>/app/<id>/current/active, as Flatpak links it.
    info_file.write_text("\n".join(x for x in info.splitlines() if "app-path" not in x))
    app = root / "var/lib/flatpak/app/org.kde.kdenlive"
    (app / "current").symlink_to("x86_64/stable")
    (app / "x86_64/stable/active").symlink_to(KDENLIVE_COMMIT)
    assert packaging_of(1202, proc, root).version == "25.08.3"

    # Same thing in the per-user installation, with the older appdata location.
    user = root / "home/tux/.local/share/flatpak/app/org.kde.kdenlive/current/active"
    shutil.move(app, root / "home/tux/.local/share/flatpak/app/org.kde.kdenlive")
    assert not app.exists()
    (user / "export/share/appdata").mkdir()
    (user / "export/share/metainfo/org.kde.kdenlive.metainfo.xml").rename(
        user / "export/share/appdata/org.kde.kdenlive.appdata.xml"
    )
    assert packaging_of(1202, proc, root) == PackageInfo(
        Packaging.FLATPAK, "org.kde.kdenlive", "25.08.3", "stable", "flatpak-metadata"
    )
    # A deployment whose metadata names another app is not this app's.
    (user / "metadata").write_text("[Application]\nname=org.other.App\n")
    assert packaging_of(1202, proc, root).version is None


def test_flatpak_id_is_validated_before_it_is_used_in_a_path(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    info_file = proc / "1202" / "root" / ".flatpak-info"
    info_file.write_text("[Application]\nname=../../../../etc\n")
    (proc / "1202" / "environ").write_bytes(b"FLATPAK_ID=../../etc\0container=flatpak\0")
    (proc / "1201" / "environ").write_bytes(b"")
    (proc / "1201" / "root" / ".flatpak-info").unlink()
    fs = CountingFs(root)
    chain = ProcReader(proc).chain(1202)
    assert detect_packaging(resolve_binary(chain), chain, fs, home=HOME) == PackageInfo(
        Packaging.FLATPAK, "", None, None, "unknown"
    )
    assert fs.paths == []


def test_flatpak_release_regex_takes_the_first_release_and_ignores_entities(
    tree: tuple[Path, Path],
) -> None:
    proc, root = tree
    xml = root / KDENLIVE_DEPLOY / "export/share/metainfo/org.kde.kdenlive.metainfo.xml"
    xml.write_text(
        '<!DOCTYPE c [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
        "<component><description>&b; release notes</description><releases>\n"
        "<release type='stable' date='2026-01-01' version='26.04.0'>\n"
        '<release version="25.12.3"/></releases></component>\n'
    )
    assert packaging_of(1202, proc, root).version == "26.04.0"
    xml.write_text('<releases><release version="&b;"/><release version="not a version"/>')
    assert packaging_of(1202, proc, root).version is None
    xml.write_text("<release version='1.2.3" + "x" * 300_000)  # never closed, over the cap
    assert packaging_of(1202, proc, root).version is None


def test_snap_from_cgroup_only_reads_the_revision_the_process_runs(
    tree: tuple[Path, Path],
) -> None:
    proc, root = tree
    assert not (PROC / "1310" / "environ").exists()
    fs = CountingFs(root)
    chain = ProcReader(proc).chain(1310)
    info = detect_packaging(resolve_binary(chain), chain, fs, home=HOME)
    assert info.version == "142.0.7444.59"
    assert fs.paths == ["/snap/chromium/3100/meta/snap.yaml"]
    # exe outside /snap (a base-snap interpreter): the name is in the cgroup, the yaml
    # under "current".
    shutil.move(root / "snap/chromium/3100", root / "snap/chromium/current")
    (proc / "1310" / "exe").unlink()
    (proc / "1310" / "exe").symlink_to("/usr/bin/python3")
    assert packaging_of(1310, proc, root) == PackageInfo(
        Packaging.SNAP, "chromium", "142.0.7444.59", None, "snap-yaml"
    )
    (root / "snap/chromium/current/meta/snap.yaml").write_text("name: chromium\nversion:\n")
    assert packaging_of(1310, proc, root) == PackageInfo(
        Packaging.SNAP, "chromium", None, None, "unknown"
    )


def test_a_scope_that_merely_mentions_snap_is_not_a_snap() -> None:
    plain = snapshot_of(
        exe="/usr/bin/gedit",
        cgroup="0::/user.slice/user-1000.slice/app-snap.scope\n1:name=x:/snapshots/a.b.c\n",
    )
    binary = resolve_binary([plain])
    assert detect_packaging(binary, [plain], RealFs(ROOT), home=HOME).kind is Packaging.SYSTEM


def test_appimage_without_a_version_in_its_name() -> None:
    snap = snapshot_of(
        exe="/tmp/.mount_ObsidiXy/obsidian", env={"APPIMAGE": "/home/tux/Obsidian.AppImage"}
    )
    binary = resolve_binary([snap])
    assert detect_packaging(binary, [snap], CountingFs(), home=HOME) == PackageInfo(
        Packaging.APPIMAGE, "Obsidian.AppImage", None, None, "appimage-name"
    )


def test_electron_version_file_is_not_mistaken_for_the_app_version(
    tree: tuple[Path, Path],
) -> None:
    proc, root = tree
    assert (root / "opt/Obsidian/version").read_text() == "37.2.4\n"
    (root / "var/lib/dpkg/info/obsidian.list").unlink()
    assert packaging_of(1500, proc, root) == PackageInfo(
        Packaging.OPT, "Obsidian", None, None, "unknown"
    )
    # The same file in a directory that is not an Electron distribution is a version file.
    (root / "opt/Obsidian/resources/app.asar").unlink()
    assert packaging_of(1500, proc, root) == PackageInfo(
        Packaging.OPT, "Obsidian", "37.2.4", None, "version-file"
    )


def test_opt_version_sources_and_their_boundaries(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    resolve_dir = root / "opt/resolve"
    # A version file one level above /opt/<name> must not be picked up.
    (root / "opt/VERSION").write_text("9.9.9\n")
    assert packaging_of(1900, proc, root).version is None
    (resolve_dir / ".version").write_text("\n\n  21.1.2  \nsecond line\n")
    assert packaging_of(1900, proc, root) == PackageInfo(
        Packaging.OPT, "resolve", "21.1.2", None, "version-file"
    )
    (resolve_dir / "bin" / "VERSION").write_text("DaVinci Resolve Studio 21.1.0.17 build\n")
    assert packaging_of(1900, proc, root).version == "21.1.0.17"  # nearest directory wins
    # product-info.json: "version", else the data directory name
    info = root / "opt/pycharm-2025.2/product-info.json"
    info.write_text('{"dataDirectoryName": "PyCharm2026.1", "version": 20261}')
    assert packaging_of(1800, proc, root) == PackageInfo(
        Packaging.OPT, "pycharm-2025.2", "2025.2", None, "dir-name"
    )
    shutil.move(root / "opt/pycharm-2025.2", root / "opt/pycharm")
    (proc / "1800" / "cmdline").write_bytes(b"/bin/sh\0/opt/pycharm/bin/pycharm.sh\0")
    assert packaging_of(1800, proc, root).version is None  # "PyCharm2026.1": no separator
    (root / "opt/pycharm/product-info.json").write_text('{"dataDirectoryName": "PyCharm-2026.1"}')
    assert packaging_of(1800, proc, root) == PackageInfo(
        Packaging.OPT, "pycharm", "2026.1", None, "version-file"
    )
    for garbage in ("[1, 2]", '"x"', "{", "[" * 100_000, '{"version": {"a": 1}}'):
        (root / "opt/pycharm/product-info.json").write_text(garbage)
        assert packaging_of(1800, proc, root).version is None


def test_tarball_in_home_never_reads_the_home_directory_itself(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    (root / "home/tux/VERSION").write_text("1.2.3\n")
    fs = CountingFs(root)
    chain = ProcReader(proc).chain(1100)
    info = detect_packaging(resolve_binary(chain), chain, fs, home=HOME)
    assert (info.version, info.source) == ("4.5.3", "dir-name")
    assert all(p.startswith("/home/tux/apps/") for p in fs.paths)
    assert fs.calls["listdir"] == 0  # no dpkg scan for a path under the home directory
    # Without a home, the same path is simply not classifiable.
    assert detect_packaging(resolve_binary(chain), chain, fs).kind is Packaging.UNKNOWN


def test_detect_packaging_never_raises_even_if_the_filesystem_does() -> None:
    for pid in ALL_PIDS:
        chain = ProcReader(PROC).chain(pid)
        binary = resolve_binary(chain)
        info = detect_packaging(binary, chain, ExplodingFs(), home=HOME)  # type: ignore[arg-type]
        assert isinstance(info, PackageInfo)
    # Kinds that need no file are still answered; the others degrade to UNKNOWN.
    chain = ProcReader(PROC).chain(1000)
    exploded = detect_packaging(resolve_binary(chain), chain, ExplodingFs(), home=HOME)  # type: ignore[arg-type]
    assert exploded == UNKNOWN_PACKAGE


def test_real_fs_reads_only_regular_files_inside_its_root(tmp_path: Path) -> None:
    (tmp_path / "root/opt/app").mkdir(parents=True)
    (tmp_path / "root/opt/app/VERSION").write_text("1.2.3\n" + "x" * 100)
    (tmp_path / "outside").write_text("secret")
    os.mkfifo(tmp_path / "root/opt/app/version")
    fs = RealFs(tmp_path / "root")
    assert fs.read_text("/opt/app/VERSION", 6) == "1.2.3\n"
    assert fs.read_text("/opt/app/version", 100) is None  # a FIFO: would block a naive open
    assert fs.read_text("/opt/app", 100) is None
    assert fs.read_text("/opt/app/../../../outside", 100) is None
    assert fs.read_text("opt/app/VERSION", 100) is None
    assert fs.read_text("/opt/app/VER\0SION", 100) is None
    assert fs.read_text("/" + "a" * 5000, 100) is None
    assert fs.exists("/opt/app") and not fs.exists("/opt/../../outside")
    assert fs.listdir("/opt/app") == ["VERSION", "version"]
    assert fs.listdir("/opt/missing") == [] and fs.listdir("/opt/app/VERSION") == []
    assert fs.stat("/opt/app/VERSION") == (
        106,
        (tmp_path / "root/opt/app/VERSION").stat().st_mtime_ns,
    )
    assert fs.stat("/opt/missing") is None
    assert list(fs.iter_lines("/opt/app/version", 100)) == []
    assert list(fs.iter_lines("/missing", 100)) == []


def test_iter_lines_caps_bytes_and_skips_over_long_lines_whole(tmp_path: Path) -> None:
    long = "Package: " + "y" * 20_000  # longer than one read: its tail must not become a line
    (tmp_path / "status").write_text(f"a\n{long}\nVersion: 1\n\nlast-without-newline")
    fs = RealFs(tmp_path)
    assert list(fs.iter_lines("/status", 1 << 20)) == ["a", "Version: 1", ""]
    assert list(fs.iter_lines("/status", 2)) == ["a"]
    assert list(fs.iter_lines("/status", 1)) == []


# --------------------------------------------------------------------------- identify()


def test_identify_system_blender() -> None:
    assert identify_fixture(1000) == AppIdentity(
        app_id="blender",
        name="blender",
        exe="/usr/bin/blender",
        packaging=Packaging.DEB,
        package_id="blender",
        version=Version(4, 5, 3),
        version_raw="2:4.5.3+dfsg-1ubuntu2",
        channel=Channel.STABLE,
        edition=None,
        locale=None,
        source="dpkg",
        confidence=0.95,
    )


@pytest.mark.parametrize(
    ("pid", "app_id", "packaging", "version", "source", "exe"),
    [
        (
            1100,
            "blender",
            Packaging.OPT,
            "4.5.3",
            "dir-name",
            "/home/tux/apps/blender-4.5.3-linux-x64/blender",
        ),
        (
            1202,
            "org.kde.kdenlive",
            Packaging.FLATPAK,
            "25.8.3",
            "flatpak-metadata",
            "/app/bin/kdenlive",
        ),
        (
            1300,
            "firefox",
            Packaging.SNAP,
            "145.0.1",
            "snap-env",
            "/snap/firefox/5187/usr/lib/firefox/firefox",
        ),
        (
            1310,
            "chromium",
            Packaging.SNAP,
            "142.0.7444",
            "snap-yaml",
            "/snap/chromium/3100/usr/lib/chromium-browser/chrome",
        ),
        (
            1400,
            "krita",
            Packaging.APPIMAGE,
            "5.2.11",
            "appimage-name",
            "/home/tux/Applications/krita-5.2.11-x86_64.AppImage",
        ),
        (1502, "obsidian", Packaging.DEB, "1.10.3", "dpkg", "/opt/Obsidian/obsidian"),
        (1600, "element", Packaging.SYSTEM, None, "unknown", "/usr/lib/element/resources/app.asar"),
        (1601, "element", Packaging.SYSTEM, None, "unknown", "/usr/lib/element/resources/app.asar"),
        (1700, "virt-manager", Packaging.DEB, "5.1.0", "dpkg", "/usr/bin/virt-manager"),
        (
            1800,
            "pycharm",
            Packaging.OPT,
            "2025.2.4",
            "version-file",
            "/opt/pycharm-2025.2/bin/pycharm.sh",
        ),
        (1900, "resolve", Packaging.OPT, None, "unknown", "/opt/resolve/bin/resolve"),
        (2001, "blender", Packaging.DEB, "4.5.3", "dpkg", "/usr/bin/blender"),
        (2200, "blender", Packaging.DEB, "4.5.3", "dpkg", "/usr/bin/blender"),
        (
            2300,
            "gimp",
            Packaging.UNKNOWN,
            None,
            "unknown",
            "/nix/store/1b9p07z77phvv2hf6gm9f28syp39f1ag-gimp-3.0.4/bin/.gimp-wrapped",
        ),
        (2500, "bash", Packaging.DEB, "5.3", "dpkg", "/usr/bin/bash"),
        (2610, "handbuilt", Packaging.SYSTEM, None, "unknown", "/usr/bin/handbuilt"),
    ],
)
def test_identify_end_to_end(
    pid: int, app_id: str, packaging: Packaging, version: str | None, source: str, exe: str
) -> None:
    identity = identify_fixture(pid)
    assert (identity.app_id, identity.packaging, identity.source, identity.exe) == (
        app_id,
        packaging,
        source,
        exe,
    )
    assert (str(identity.version) if identity.version else None) == version
    assert (identity.version_raw == "") == (version is None)
    assert identity.locale is None
    assert identity.confidence == pytest.approx(
        {"dpkg": 0.95, "flatpak-metadata": 0.95, "snap-yaml": 0.95, "snap-env": 0.9}.get(
            source, {"version-file": 0.85, "dir-name": 0.6, "appimage-name": 0.6}.get(source, 0)
        )
        or (0.1 if packaging is Packaging.UNKNOWN else 0.3)
    )


def test_app_id_preference_order() -> None:
    # Flatpak id and snap name beat KWin's desktop file; that beats the binary name.
    hint = {"desktop_file": "org.kde.something-else", "window_class": "whatever"}
    assert identify_fixture(1202, **hint).app_id == "org.kde.kdenlive"
    assert identify_fixture(1300, **hint).app_id == "firefox"
    assert identify_fixture(1000, **hint).app_id == "org.kde.something-else"
    assert identify_fixture(1000, window_class="Blender").app_id == "blender"
    assert (
        identify_fixture(
            1000, desktop_file="/usr/share/applications/org.blender.Blender.desktop"
        ).app_id
        == "org.blender.Blender"
    )
    # A desktop file name that is not id-shaped is ignored, not sanitised into something.
    for bad in ("../../etc/passwd\n", "a b c", "x" * 300, "\x1b[31m"):
        assert identify_fixture(1000, desktop_file=bad).app_id == "blender"
    # No readable binary: the window's own names are all there is.
    assert identify_fixture(2800, window_class="Some App!").app_id == "some-app"
    assert identify_fixture(2800, desktop_file="org.x.App", window_class="y").app_id == "org.x.App"


def test_identify_unknown_process() -> None:
    assert identify_fixture(2000) == UNKNOWN_IDENTITY
    assert identify_fixture(99999, desktop_file="org.kde.kate") == dataclasses.replace(
        UNKNOWN_IDENTITY, app_id="org.kde.kate"
    )
    assert identify_fixture(-5) == UNKNOWN_IDENTITY
    assert (
        identify(1000, ProcReader(PROC), ExplodingFs(), HOME, None).packaging is Packaging.UNKNOWN
    )  # type: ignore[arg-type]


def test_edition_is_only_reported_when_something_names_it(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    assert identify_fixture(1900).edition is None
    assert identify_fixture(1900, window_class="DaVinci Resolve Studio").edition == "Studio"
    assert identify_fixture(1900, window_class="resolve").edition is None
    assert identify_fixture(1000, window_class="Blender Studio").edition is None
    studio = root / "home/tux/DaVinci_Resolve_Studio_21.1.2_Linux/bin"
    studio.mkdir(parents=True)
    (studio / "resolve").write_bytes(b"\x7fELF")
    (proc / "1900" / "exe").unlink()
    (proc / "1900" / "exe").symlink_to("/home/tux/DaVinci_Resolve_Studio_21.1.2_Linux/bin/resolve")
    identity = identify(1900, ProcReader(proc), RealFs(root), HOME, None)
    assert (identity.edition, str(identity.version), identity.source) == (
        "Studio",
        "21.1.2",
        "dir-name",
    )


def test_channel_from_version_text_and_flatpak_branch(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    alpha = root / "home/tux/apps/blender-5.4.0-alpha+main.a1b2c3d4e5f6-linux.x86_64-release"
    shutil.move(root / "home/tux/apps/blender-4.5.3-linux-x64", alpha) if (
        root / "home/tux/apps/blender-4.5.3-linux-x64"
    ).exists() else alpha.mkdir(parents=True)
    (proc / "1100" / "exe").unlink()
    (proc / "1100" / "exe").symlink_to(f"/home/tux/apps/{alpha.name}/blender")
    identity = identify(1100, ProcReader(proc), RealFs(root), HOME, None)
    assert (identity.app_id, identity.channel, identity.version_raw) == (
        "blender",
        Channel.ALPHA,
        "5.4.0-alpha",
    )
    assert identity.version is not None and identity.version.pre == "alpha"

    info_file = proc / "1202" / "root" / ".flatpak-info"
    text = info_file.read_text()
    assert identify(1202, ProcReader(proc), RealFs(root), HOME, None).channel is Channel.STABLE
    info_file.write_text(text.replace("branch=stable", "branch=beta"))
    assert identify(1202, ProcReader(proc), RealFs(root), HOME, None).channel is Channel.BETA


# --------------------------------------------------------------------------- cache.py

IDENTITIES = [
    UNKNOWN_IDENTITY,
    AppIdentity(
        app_id="org.kde.kdenlive",
        name="kdenlive",
        exe="/app/bin/kdenlive",
        packaging=Packaging.FLATPAK,
        package_id="org.kde.kdenlive",
        version=Version(25, 8, None, "rc"),
        version_raw="25.08~rc1",
        channel=Channel.RC,
        edition=None,
        locale=None,
        source="flatpak-metadata",
        confidence=0.95,
    ),
    AppIdentity(
        app_id="resolve",
        name='resolve "ü" \u2028\\',
        exe="/opt/resolve/bin/resolve",
        packaging=Packaging.OPT,
        package_id="resolve",
        version=Version(21, 1, 0),
        version_raw="21.1.0.17",
        channel=Channel.STABLE,
        edition="Studio",
        locale=None,
        source="version-file",
        confidence=0.1 + 0.2,  # not exactly representable: must still survive unchanged
    ),
]


@pytest.mark.parametrize("identity", IDENTITIES)
def test_identity_json_round_trip_is_exact(identity: AppIdentity, tmp_path: Path) -> None:
    again = AppIdentity.from_json(identity.to_json())
    # repr, because Version equality deliberately ignores "patch missing" vs "patch 0".
    assert repr(again) == repr(identity) and again == identity
    assert type(again.packaging) is Packaging and type(again.channel) is Channel
    cache = IdentityCache(tmp_path / "id.sqlite")
    cache.put("k", identity)
    cached = cache.get("k")
    assert repr(cached) == repr(identity)
    assert cache.get("other") is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "null",
        "[]",
        "{}",
        '{"format": 2}',
        "{" * 50_000,
        IDENTITIES[1].to_json()[:-10],
        IDENTITIES[1].to_json().replace('"flatpak"', '"msi"'),
        IDENTITIES[1].to_json().replace('"rc"', '"gamma"'),
        IDENTITIES[1].to_json().replace('"major":25', '"major":true'),
        IDENTITIES[1].to_json().replace('"major":25', '"major":-25'),
        IDENTITIES[1].to_json().replace('"confidence":0.95', '"confidence":NaN'),
        IDENTITIES[1].to_json().replace('"confidence":0.95', '"confidence":7'),
        IDENTITIES[1].to_json().replace('"confidence":0.95', '"confidence":"0.95"'),
        IDENTITIES[1].to_json().replace('"locale":null', '"locale":"sv"'),
        IDENTITIES[1].to_json().replace('"name":"kdenlive"', '"name":["kdenlive"]'),
        IDENTITIES[1].to_json().replace('"name":"kdenlive"', '"name":"' + "k" * 5000 + '"'),
    ],
)
def test_from_json_rejects_anything_that_is_not_an_identity(text: str) -> None:
    with pytest.raises(ValueError):
        AppIdentity.from_json(text)


def test_cache_file_is_private_and_persistent(tmp_path: Path) -> None:
    path = tmp_path / "state" / "identity.sqlite"
    old_umask = os.umask(0)
    try:
        cache = IdentityCache(path)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    cache.put("a", IDENTITIES[1])
    cache.put("b", IDENTITIES[2])
    cache.close()
    assert cache.get("a") is None  # closed: a no-op, not an error
    cache.put("a", IDENTITIES[1])

    reopened = IdentityCache(path)
    assert reopened.get("a") == IDENTITIES[1]
    db = sqlite3.connect(path)
    columns = [(row[1], row[2], row[5]) for row in db.execute("PRAGMA table_info(identity)")]
    assert columns == [("key", "TEXT", 1), ("json", "TEXT", 0), ("created_ms", "INT", 0)]
    created = db.execute("SELECT created_ms FROM identity WHERE key = 'a'").fetchone()[0]
    assert abs(created - time.time() * 1000) < 60_000
    db.close()
    reopened.forget()
    assert reopened.get("a") is None and reopened.get("b") is None
    # A world-readable file left by something else is tightened on open.
    reopened.close()
    path.chmod(0o644)
    IdentityCache(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "garbage", ["", "not json", "{}", '{"format":1}', b"\xff\x00blob", 12, None]
)
def test_corrupt_rows_are_dropped_not_raised(tmp_path: Path, garbage: object) -> None:
    path = tmp_path / "id.sqlite"
    cache = IdentityCache(path)
    cache.put("good", IDENTITIES[1])
    db = sqlite3.connect(path)
    db.execute("INSERT INTO identity VALUES ('bad', ?, 0)", (garbage,))
    db.commit()
    assert cache.get("bad") is None
    assert db.execute("SELECT count(*) FROM identity WHERE key = 'bad'").fetchone()[0] == 0
    assert cache.get("good") == IDENTITIES[1]
    db.close()


def test_a_corrupt_or_unusable_database_degrades_to_no_cache(tmp_path: Path) -> None:
    path = tmp_path / "id.sqlite"
    path.write_bytes(random.Random(1).randbytes(8192))
    cache = IdentityCache(path)
    cache.put("k", IDENTITIES[1])
    assert cache.get("k") is None
    cache.forget()
    directory = IdentityCache(tmp_path)  # a directory, not a file
    directory.put("k", IDENTITIES[1])
    assert directory.get("k") is None
    assert identify(1000, ProcReader(PROC), RealFs(ROOT), HOME, cache).package_id == "blender"


def test_cache_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("waddle_desktop.identity.cache._MAX_ROWS", 3)
    cache = IdentityCache(tmp_path / "id.sqlite")
    for index in range(6):
        cache.put(f"k{index}", IDENTITIES[1])
    assert [cache.get(f"k{i}") is not None for i in range(6)] == [False] * 3 + [True] * 3


def test_cache_key_changes_with_whatever_an_update_changes() -> None:
    base = cache_key("blender", "/usr/bin/blender", exe_stat=(10, 20))
    assert base is not None
    others = {
        cache_key("blender", "/usr/bin/blender", exe_stat=(11, 20)),
        cache_key("blender", "/usr/bin/blender", exe_stat=(10, 21)),
        cache_key("blender", "/usr/bin/blender2", exe_stat=(10, 20)),
        cache_key("blender2", "/usr/bin/blender", exe_stat=(10, 20)),
        cache_key("blender", "/usr/bin/blender", package_version="4.5.3", exe_stat=(10, 20)),
        cache_key("blender", "/usr/bin/blender", package_version="4.5.4", exe_stat=(10, 20)),
        cache_key('blender","/usr', "/bin/blender", exe_stat=(10, 20)),
    }
    assert len(others) == 7 and base not in others
    assert cache_key("blender", "/usr/bin/blender") is None  # nothing to invalidate on


@pytest.mark.parametrize(
    ("pid", "hit_calls"),
    [
        (1000, {"stat": 1}),  # deb: one stat of the exe
        (1700, {"stat": 1}),  # the script, not the interpreter
        (1400, {"stat": 1}),  # the AppImage file
        (1202, {}),  # Flatpak: the commit is in the snapshot
        (1300, {}),  # snap: so is the revision
    ],
)
def test_cache_hit_touches_the_filesystem_at_most_once(
    tmp_path: Path, pid: int, hit_calls: dict[str, int]
) -> None:
    cache = IdentityCache(tmp_path / "id.sqlite")
    cold_fs = CountingFs()
    cold = identify(pid, ProcReader(PROC), cold_fs, HOME, cache)
    assert cold.version is not None
    if pid not in (1300, 1400):  # those came from the environment / the file name
        assert sum(cold_fs.calls.values()) > sum(hit_calls.values())
    warm_fs = CountingFs()
    warm = identify(pid, ProcReader(PROC), warm_fs, HOME, cache)
    assert dict(warm_fs.calls) == hit_calls
    assert repr(warm) == repr(cold)
    if hit_calls:
        assert warm_fs.paths == [cold.exe]
    # Without a cache the same call has to do the work again.
    uncached_fs = CountingFs()
    identify(pid, ProcReader(PROC), uncached_fs, HOME, None)
    assert uncached_fs.calls == cold_fs.calls


def test_cache_is_invalidated_when_the_exe_changes(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    cache = IdentityCache(root.parent / "id.sqlite")
    reader, fs = ProcReader(proc), RealFs(root)
    assert identify(1000, reader, fs, HOME, cache).version_raw == "2:4.5.3+dfsg-1ubuntu2"

    status = root / "var/lib/dpkg/status"
    status.write_text(status.read_text().replace("2:4.5.3+dfsg-1ubuntu2", "2:4.5.4+dfsg-1"))
    # The exe is untouched, so this is still the cached answer: the key is the contract.
    assert identify(1000, reader, fs, HOME, cache).version_raw == "2:4.5.3+dfsg-1ubuntu2"

    exe = root / "usr/bin/blender"
    before = exe.stat()
    exe.write_bytes(exe.read_bytes() + b"-upgraded")  # size changes
    assert identify(1000, reader, fs, HOME, cache).version == Version(4, 5, 4)

    status.write_text(status.read_text().replace("2:4.5.4+dfsg-1", "2:4.5.5+dfsg-1"))
    size = exe.stat().st_size
    os.utime(exe, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))  # mtime only
    assert exe.stat().st_size == size
    assert identify(1000, reader, fs, HOME, cache).version == Version(4, 5, 5)
    cache.forget()
    assert identify(1000, reader, fs, HOME, cache).version == Version(4, 5, 5)


def test_cache_is_invalidated_when_the_package_version_changes(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    cache = IdentityCache(root.parent / "id.sqlite")
    reader, fs = ProcReader(proc), RealFs(root)
    assert str(identify(1202, reader, fs, HOME, cache).version) == "25.8.3"
    assert str(identify(1300, reader, fs, HOME, cache).version) == "145.0.1"

    # Flatpak update: a new commit directory, and the running instance says so.
    new_commit = "ab" * 32
    old = root / KDENLIVE_DEPLOY
    new = old.with_name(new_commit)
    shutil.copytree(old, new)
    xml = new / "export/share/metainfo/org.kde.kdenlive.metainfo.xml"
    xml.write_text(xml.read_text().replace('version="25.08.3"', 'version="25.12.0"'))
    for pid in ("1201", "1202"):
        info = proc / pid / "root" / ".flatpak-info"
        info.write_text(info.read_text().replace(KDENLIVE_COMMIT, new_commit))
    assert str(identify(1202, reader, fs, HOME, cache).version) == "25.12.0"

    # Snap refresh: new revision and version in the environment of the new process.
    environ = proc / "1300" / "environ"
    environ.write_bytes(
        environ.read_bytes()
        .replace(b"SNAP_REVISION=5187", b"SNAP_REVISION=5200")
        .replace(b"SNAP_VERSION=145.0.1-1", b"SNAP_VERSION=146.0-2")
    )
    assert str(identify(1300, reader, fs, HOME, cache).version) == "146.0"


def test_identity_logging_never_carries_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(log, "event", lambda name, **fields: lines.append((name, fields)))
    monkeypatch.setattr(log, "warning", lambda name, **fields: lines.append((name, fields)))
    path = tmp_path / "id.sqlite"
    cache = IdentityCache(path)
    for pid in ALL_PIDS:
        identify(pid, ProcReader(PROC), RealFs(ROOT), HOME, cache, window_class="Secret Title")
    db = sqlite3.connect(path)
    db.execute("UPDATE identity SET json = 'garbage'")
    db.commit()
    db.close()
    identify(1000, ProcReader(PROC), RealFs(ROOT), HOME, cache)
    assert [name for name, _ in lines] == ["identity.cache_row_dropped"]
    assert all(not fields for _, fields in lines)


# --------------------------------------------------------------------------- robustness


def _regular_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*") if p.is_file() and not p.is_symlink())


def test_nothing_raises_on_garbage_in_any_fixture_file(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    files = _regular_files(proc) + _regular_files(root)
    assert len(files) > 100
    rng = random.Random(20261112)
    reader, fs = ProcReader(proc), RealFs(root)
    pids_by_dir = {
        int(p.name): [int(p.name), *(c for c in ALL_PIDS if c // 100 == int(p.name) // 100)]
        for p in proc.iterdir()
    }
    for path in files:
        original = path.read_bytes()
        related = (
            pids_by_dir.get(int(path.relative_to(proc).parts[0])) if proc in path.parents else None
        )
        for variant in range(3):
            if variant == 0:
                data = rng.randbytes(rng.choice([1, 17, 4096, 70_000]))
            elif variant == 1:
                data = original[: rng.randrange(len(original) + 1)]  # truncated
            else:
                mutable = bytearray(original or b"\0")
                for _ in range(8):
                    mutable[rng.randrange(len(mutable))] = rng.randrange(256)
                data = bytes(mutable) + rng.randbytes(16)
            path.write_bytes(data)
            cache = IdentityCache(root.parent / "fuzz.sqlite")
            for pid in related or ALL_PIDS:
                chain = reader.chain(pid)
                binary = resolve_binary(chain)
                assert isinstance(identity_name(binary), str)
                assert isinstance(detect_packaging(binary, chain, fs, home=HOME), PackageInfo)
                first = identify(pid, reader, fs, HOME, cache, desktop_file="x.desktop")
                assert isinstance(first, AppIdentity)
                assert repr(
                    identify(pid, reader, fs, HOME, cache, desktop_file="x.desktop")
                ) == repr(first)
                AppIdentity.from_json(first.to_json())
            cache.close()
        path.write_bytes(original)
    # The tree is intact again, so a wrong answer above was not masked by leftover damage.
    assert identify(1000, reader, fs, HOME, None).version == Version(4, 5, 3)


def test_every_fixture_file_is_garbage_at_once(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    rng = random.Random(7)
    for path in _regular_files(proc) + _regular_files(root):
        path.write_bytes(rng.randbytes(rng.randrange(0, 3000)))
    cache = IdentityCache(root.parent / "id.sqlite")
    for pid in ALL_PIDS:
        identity = identify(pid, ProcReader(proc), RealFs(root), HOME, cache)
        assert isinstance(identity, AppIdentity)
        assert identity.version is None or identity.source != "unknown"


@pytest.mark.parametrize("seed", range(40))
def test_random_text_never_breaks_the_string_parsers(seed: int) -> None:
    rng = random.Random(seed)
    alphabet = "0123456789.-_~+:vVabrc \n\t/\\()[]=\"'<>\0é\ufffd" + "alphabetarcnightlyLTSstudio"
    text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 400)))
    version = parse_version(text)
    assert version is None or isinstance(version, Version)
    raw = version_text(text)
    assert (raw is None) == (version is None)
    if raw is not None:
        assert repr(parse_version(raw)) == repr(version)
    assert isinstance(detect_channel(text), Channel)
    detect_edition(text, [text, text[::-1]])
    snap = snapshot_of(
        exe=text[:50] or None,
        cmdline=tuple(text.split(" ")),
        env={"APPIMAGE": text, "SNAP_NAME": text[:8], "FLATPAK_ID": text[:20], "APPDIR": text[:9]},
        cgroup=text,
        root_flatpak_info=text,
    )
    binary = resolve_binary([snap, snap])
    assert isinstance(identity_name(binary), str)
    assert isinstance(detect_packaging(binary, [snap], RealFs(ROOT), home=HOME), PackageInfo)


# --------------------------------------------------------------------------- performance


def test_cached_lookup_is_fast(tmp_path: Path) -> None:
    cache = IdentityCache(tmp_path / "id.sqlite")
    reader, fs = ProcReader(PROC), RealFs(ROOT)
    cold = identify(1502, reader, fs, HOME, cache)
    assert cold.version == Version(1, 10, 3)
    timings = []
    for _ in range(50):
        start = time.perf_counter()
        warm = identify(1502, reader, fs, HOME, cache)  # a four-process chain
        timings.append(time.perf_counter() - start)
        assert warm == cold
    assert statistics.median(timings) < 0.005


def test_cold_deb_lookup_over_300_list_files_is_fast(tree: tuple[Path, Path]) -> None:
    proc, root = tree
    info = root / "var/lib/dpkg/info"
    (info / "blender.list").rename(info / "zz-graphics-suite.list")  # no name hint: scanned last
    lines = "".join(f"/usr/share/pkg/file-{n:04d}.dat\n" for n in range(400))
    for index in range(300):
        (info / f"lib{index:03d}:amd64.list").write_text(f"/.\n/usr/lib/lib{index}.so\n" + lines)
        (info / f"lib{index:03d}:amd64.md5sums").write_text("0  x\n")
    status = root / "var/lib/dpkg/status"
    filler = "".join(
        f"Package: lib{i:03d}\nStatus: install ok installed\nVersion: 1.{i}\nDescription: x\n\n"
        for i in range(300)
    )
    status.write_text(
        filler + status.read_text().replace("Package: blender\n", "Package: zz-graphics-suite\n")
    )
    best = float("inf")
    for _ in range(3):
        fs = CountingFs(root)
        start = time.perf_counter()
        identity = identify(1000, ProcReader(proc), fs, HOME, None)
        best = min(best, time.perf_counter() - start)
        assert (identity.package_id, identity.version) == ("zz-graphics-suite", Version(4, 5, 3))
        assert fs.calls["read_text"] > 300  # the scan really went through the other lists
    assert best < 0.100


# --------------------------------------------------------------------------- hostile /proc


def _fake_process(proc: Path, pid: int, *, exe: bytes | None = None, cmdline: bytes = b"") -> Path:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    (directory / "stat").write_bytes(b"%d (x) S 1 1 1" % pid)
    (directory / "cmdline").write_bytes(cmdline)
    (directory / "environ").write_bytes(b"")
    (directory / "cgroup").write_bytes(b"")
    if exe is not None:
        os.symlink(exe, os.fsencode(directory / "exe"))  # noqa: PTH211 - a bytes target
    return directory


def _fifo_with_data(path: Path, data: bytes) -> int:
    os.mkfifo(path)
    writer = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    os.write(writer, data)
    return writer


def _pending(fd: int) -> bytes:
    try:
        return os.read(fd, 4096)
    except BlockingIOError:
        return b""


def test_a_fifo_posing_as_a_process_file_is_never_read(tmp_path: Path) -> None:
    # Reading a FIFO takes the data away from whoever it was meant for: that would change
    # another program's state (I1). A sandboxed process controls what its /.flatpak-info is.
    proc = tmp_path / "proc"
    directory = _fake_process(proc, 12, exe=b"/usr/bin/x")
    (directory / "root").mkdir()
    payload = b"[Application]\nname=org.evil.App\n"
    info = _fifo_with_data(directory / "root" / ".flatpak-info", payload)
    (directory / "cgroup").unlink()
    cgroup = _fifo_with_data(directory / "cgroup", b"0::/snap.evil.app-1.scope\n")
    try:
        snap = ProcReader(proc).read(12)
        assert snap is not None
        assert snap.root_flatpak_info is None and snap.cgroup == ""
        assert _pending(info) == payload
        assert _pending(cgroup) == b"0::/snap.evil.app-1.scope\n"
    finally:
        os.close(info)
        os.close(cgroup)


def test_a_symlinked_flatpak_info_is_not_followed(tmp_path: Path) -> None:
    # Inside its own mount namespace a process can point /.flatpak-info at any file.
    proc = tmp_path / "proc"
    directory = _fake_process(proc, 13, exe=b"/usr/bin/x")
    (directory / "root").mkdir()
    (tmp_path / "private").write_text("[Application]\nname=org.secret.Leak\n")
    (directory / "root" / ".flatpak-info").symlink_to(tmp_path / "private")
    snap = ProcReader(proc).read(13)
    assert snap is not None and snap.root_flatpak_info is None
    chain = ProcReader(proc).chain(13)
    assert detect_packaging(resolve_binary(chain), chain, RealFs(ROOT), home=HOME).kind is not (
        Packaging.FLATPAK
    )


def test_version_file_that_is_a_fifo_is_left_alone(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "opt/evil/bin").mkdir(parents=True)
    (root / "opt/evil/bin/evil").write_bytes(b"\x7fELF")
    writer = _fifo_with_data(root / "opt/evil/VERSION", b"9.9.9\n")
    (root / "opt/evil/version").symlink_to("version")  # a loop
    (root / "opt/evil/version.txt").symlink_to("/dev/zero")  # endless, not a regular file
    proc = tmp_path / "proc"
    _fake_process(proc, 17, exe=b"/opt/evil/bin/evil")
    try:
        identity = identify(17, ProcReader(proc), RealFs(root), HOME, None)
        assert (identity.packaging, identity.version) == (Packaging.OPT, None)
        assert _pending(writer) == b"9.9.9\n"
    finally:
        os.close(writer)


def test_a_non_utf8_or_control_character_exe_path_yields_clean_text_and_still_caches(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    app = root / "opt"
    binary = os.fsencode(app) + b"/caf\xe9/bin/caf\xe9"  # not UTF-8: Latin-1 e-acute
    Path(os.fsdecode(binary)).parent.mkdir(parents=True)
    Path(os.fsdecode(binary)).write_bytes(b"\x7fELF")
    proc = tmp_path / "proc"
    # The link target exists, so the snapshot has an exe_stat and the lookup goes to the cache.
    _fake_process(proc, 10, exe=binary)
    _fake_process(proc, 16, exe=b"/opt/a\nb\x1b[31m\xe2\x80\xae/bin/app")
    cache = IdentityCache(tmp_path / "id.sqlite")
    for pid in (10, 16):
        for _ in range(2):  # cold, then through the cache
            identity = identify(pid, ProcReader(proc), RealFs(root), HOME, cache)
            assert identity != UNKNOWN_IDENTITY
            for text in (identity.app_id, identity.name, identity.exe, identity.package_id):
                text.encode("utf-8")  # no lone surrogates: safe to send, store and log
                assert text.isprintable()
            assert AppIdentity.from_json(identity.to_json()) == identity
    snap = ProcReader(proc).read(10)
    assert snap is not None and snap.exe is not None
    snap.exe.encode("utf-8")
    cache.close()


@pytest.mark.parametrize("key", ['["a","\udce9","stat:1:1"]', "\ud800", "plain"])
def test_cache_never_raises_on_keys_sqlite_cannot_encode(tmp_path: Path, key: str) -> None:
    cache = IdentityCache(tmp_path / "id.sqlite")
    assert cache.get(key) is None
    cache.put(key, UNKNOWN_IDENTITY)
    cache.put("ok", dataclasses.replace(UNKNOWN_IDENTITY, exe="\udce9"))
    assert cache.get(key) in (None, UNKNOWN_IDENTITY)
    cache.close()


def test_the_environment_read_stops_at_its_cap(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    directory = _fake_process(proc, 20, exe=b"/usr/bin/x")
    filler = b"".join(b"PAD%05d=%s\0" % (n, b"x" * 1000) for n in range(3000))  # about 3 MiB
    late = b"APPIMAGE=/late.AppImage\0"
    (directory / "environ").write_bytes(b"SNAP_NAME=early\0" + filler + late)
    snap = ProcReader(proc).read(20)
    assert snap is not None
    assert dict(snap.env) == {"SNAP_NAME": "early"}  # what lies beyond 2 MiB is never read
