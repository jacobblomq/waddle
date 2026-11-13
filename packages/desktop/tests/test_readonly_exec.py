"""Sandboxed observation: the argv builder, the runner against a stand-in bwrap, and the
real sandbox where this machine has one."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from waddle_desktop import log
from waddle_desktop.sandbox import profiles, readonly_exec
from waddle_desktop.sandbox.profiles import Limits, sandbox_argv
from waddle_desktop.sandbox.readonly_exec import ReadOnlyExec, Reason, SandboxStatus

LIMITS = Limits(address_space_bytes=1000, cpu_seconds=2, file_size_bytes=30, processes=4)

# ---------------------------------------------------------------- profiles (pure)


@pytest.fixture
def fake_root(tmp_path: Path) -> Path:
    """A merged-usr system with one alternatives directory and nothing else."""
    root = tmp_path / "root"
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "bin").symlink_to("usr/bin")
    (root / "lib64").symlink_to("usr/lib64")
    (root / "etc" / "alternatives").mkdir(parents=True)
    (root / "etc" / "ld.so.cache").write_bytes(b"")
    (root / "etc" / "passwd").write_text("root:x:0:0::/root:/bin/sh\n")
    (root / "home" / "someone").mkdir(parents=True)
    return root


def test_the_profile_is_exactly_this_argv(fake_root: Path) -> None:
    argv = sandbox_argv(
        bwrap="/usr/bin/bwrap",
        prlimit="/usr/bin/prlimit",
        command=["man", "--", "ls"],
        limits=LIMITS,
        env={"PAGER": "cat"},
        ro_binds=[("/opt/app", "/opt/app")],
        root=fake_root,
    )
    assert argv == [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        *("--cap-drop", "ALL"),
        "--clearenv",
        *("--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin"),
        *("--setenv", "HOME", "/home/sandbox"),
        *("--setenv", "LANG", "C.UTF-8"),
        *("--setenv", "LC_ALL", "C.UTF-8"),
        *("--setenv", "TERM", "dumb"),
        *("--setenv", "PAGER", "cat"),
        *("--ro-bind", "/usr", "/usr"),
        *("--symlink", "usr/bin", "/bin"),
        *("--symlink", "usr/lib64", "/lib64"),
        *("--ro-bind", "/etc/alternatives", "/etc/alternatives"),
        *("--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"),
        *("--proc", "/proc"),
        *("--dev", "/dev"),
        *("--size", "8388608", "--tmpfs", "/tmp"),
        *("--size", "8388608", "--tmpfs", "/home/sandbox"),
        *("--size", "8388608", "--tmpfs", "/dev/shm"),
        *("--ro-bind", "/opt/app", "/opt/app"),
        *("--remount-ro", "/dev"),
        *("--remount-ro", "/"),
        *("--chdir", "/"),
        "--",
        "/usr/bin/prlimit",
        "--as=1000",
        "--cpu=2",
        "--fsize=30",
        "--nproc=4",
        "--",
        "man",
        "--",
        "ls",
    ]


def test_the_profile_shares_nothing_writable_and_no_session(fake_root: Path) -> None:
    argv = sandbox_argv(bwrap="b", prlimit="p", command=["x"], limits=LIMITS, root=fake_root)
    options = argv[: argv.index("--")]
    writable = {"--bind", "--bind-try", "--dev-bind", "--dev-bind-try", "--share-net"}
    assert not writable & set(options)
    joined = " ".join(options).lower()
    for word in ("dbus", "wayland", "xdg_runtime_dir", "/run", "display", "someone", "passwd"):
        assert word not in joined
    names = [options[i + 1] for i, o in enumerate(options) if o == "--setenv"]
    assert names == ["PATH", "HOME", "LANG", "LC_ALL", "TERM"]


def test_only_system_paths_that_exist_are_bound(tmp_path: Path) -> None:
    (tmp_path / "usr").mkdir()
    (tmp_path / "lib").mkdir()
    assert profiles.system_binds(tmp_path) == [
        *("--ro-bind", "/usr", "/usr"),
        *("--ro-bind", "/lib", "/lib"),
    ]
    assert profiles.system_binds(tmp_path / "missing") == []


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("de_DE.UTF-8", "de_DE.UTF-8"),
        ("C", "C"),
        ("sr_RS.utf8@latin", "sr_RS.utf8@latin"),
        ("en_US.UTF-8\nLD_PRELOAD=x", "C.UTF-8"),
        ("../../etc", "C.UTF-8"),
        ("", "C.UTF-8"),
        (None, "C.UTF-8"),
    ],
)
def test_only_plain_locale_names_are_passed_on(given: str | None, expected: str) -> None:
    assert profiles.safe_locale(given) == expected
    argv = sandbox_argv(bwrap="b", prlimit="p", command=["x"], limits=LIMITS, locale=given or "")
    assert argv[argv.index("LANG") + 1] == expected
    assert argv[argv.index("LC_ALL") + 1] == expected


def test_the_first_plain_locale_wins() -> None:
    assert profiles.safe_locale(None, "bad value", "fr_FR.UTF-8", "de_DE") == "fr_FR.UTF-8"


def test_bad_variable_names_and_empty_commands_are_refused() -> None:
    with pytest.raises(ValueError, match="variable"):
        sandbox_argv(bwrap="b", prlimit="p", command=["x"], limits=LIMITS, env={"a=b": "1"})
    with pytest.raises(ValueError, match="variable"):
        sandbox_argv(bwrap="b", prlimit="p", command=["x"], limits=LIMITS, env={"A": "1\0"})
    with pytest.raises(ValueError, match="empty"):
        sandbox_argv(bwrap="b", prlimit="p", command=[], limits=LIMITS)


def test_probe_binds_never_expose_more_than_the_app() -> None:
    assert profiles.probe_binds(PurePosixPath("/usr/bin/kate")) == []
    assert profiles.probe_binds(PurePosixPath("/opt/blender/bin/blender")) == [
        ("/opt/blender", "/opt/blender")
    ]
    assert profiles.probe_binds(PurePosixPath("/opt/tool")) == [("/opt/tool", "/opt/tool")]
    assert profiles.probe_binds(PurePosixPath("/home/me/apps/x/x")) == [
        ("/home/me/apps/x/x", "/home/me/apps/x/x")
    ]
    for bad in ("relative/x", "/opt/../home/me/x"):
        with pytest.raises(ValueError, match="absolute"):
            profiles.probe_binds(PurePosixPath(bad))


def test_tldr_caches_are_bound_only_when_they_are_real_directories_in_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".cache" / "tealdeer").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (home / ".cache" / "tldr").symlink_to(tmp_path / "elsewhere")
    (home / ".tldr").write_text("a file, not a directory")
    assert profiles.tldr_cache_binds(home) == [
        (str(home / ".cache" / "tealdeer"), "/home/sandbox/.cache/tealdeer")
    ]
    assert profiles.tldr_cache_binds(tmp_path / "no-such-home") == []


# ---------------------------------------------------------------- runner, stand-in bwrap

STAND_IN = r"""#!{python} -I
import json, os, sys, time

here = os.path.dirname(os.path.abspath(__file__))
read = lambda name: open(os.path.join(here, name)).read().strip()
mode = read("mode")
if sys.argv[-1] == "--version" and sys.argv[-2].endswith("prlimit"):  # the sandbox self-check
    with open(os.path.join(here, "checks"), "a") as f:
        f.write("x")
    sys.exit(1 if mode == "broken" else 0)
record = {{
    "argv": sys.argv,
    "env": open("/proc/self/environ", "rb").read().decode(),
    "stdin": os.readlink("/proc/self/fd/0"),
    "stdout": os.readlink("/proc/self/fd/1"),
    "stderr": os.readlink("/proc/self/fd/2"),
    "leads_own_session": os.getsid(0) == os.getpid(),
    "pid": os.getpid(),
}}
if mode in ("hang", "silent_hang"):
    child = os.fork()
    if child == 0:
        time.sleep(60)
        os._exit(0)
    record["child"] = child
with open(os.path.join(here, "record.json"), "w") as f:
    json.dump(record, f)
if mode == "ok":
    sys.stdout.write("page \x1b[31mtext‮ here\n")
elif mode == "fail":
    sys.stdout.write("partial output\n")
    sys.exit(3)
elif mode == "both":
    os.write(2, b"to-stderr\n")
    os.write(1, b"to-stdout\n")
elif mode == "multibyte":
    os.write(1, b"a" + "\u00e9".encode() * 4000)
    time.sleep(60)
elif mode == "flood":
    while True:
        os.write(1, b"x" * 65536)
elif mode == "hang":
    os.write(1, b"started\n")
    time.sleep(60)
elif mode == "silent_hang":
    os.close(1)
    os.close(2)
    time.sleep(60)
"""


class StandIn:
    """A script in place of bwrap that records how it was started and then misbehaves."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.bwrap = directory / "bwrap"
        self.prlimit = directory / "prlimit"
        self.bwrap.write_text(STAND_IN.format(python=sys.executable))
        self.bwrap.chmod(0o755)
        self.prlimit.write_text("#!/bin/false\n")
        self.prlimit.chmod(0o755)
        self.mode("ok")

    def mode(self, mode: str) -> None:
        (self.directory / "mode").write_text(mode)

    @property
    def ran(self) -> bool:
        return (self.directory / "record.json").exists()

    @property
    def record(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads((self.directory / "record.json").read_text())
        return loaded

    @property
    def checks(self) -> int:
        path = self.directory / "checks"
        return len(path.read_text()) if path.exists() else 0

    def exec(self, home: Path | None = None) -> ReadOnlyExec:
        return ReadOnlyExec(bwrap=self.bwrap, prlimit=self.prlimit, home=home or self.directory)


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StandIn:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("man", "tldr"):
        (tools / name).write_text("#!/bin/false\n")
        (tools / name).chmod(0o755)
    monkeypatch.setattr(readonly_exec, "TOOL_DIRS", (str(tools),))
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    directory = tmp_path / "sandbox-tools"
    directory.mkdir()
    return StandIn(directory)


@pytest.fixture
def spawn_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every attempt to start a process from the module, recorded and then carried out."""
    calls: list[tuple[Any, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(readonly_exec.asyncio, "create_subprocess_exec", spy)
    return calls


@pytest.fixture
def an_exe(tmp_path: Path) -> Path:
    exe = (tmp_path / "apps").resolve() / "tool"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\necho tool 1.2.3\n")
    exe.chmod(0o755)
    return exe


def state_of(pid: int) -> str:
    """'gone', or the one-letter process state ('Z' is dead but not yet collected)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return "gone"


def wait_dead(pid: int) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if state_of(pid) in ("gone", "Z"):
            return True
        time.sleep(0.02)
    return False


def expected(stand_in: StandIn, command: list[str], limits: Limits, **more: Any) -> list[str]:
    return sandbox_argv(
        bwrap=str(stand_in.bwrap),
        prlimit=str(stand_in.prlimit),
        command=command,
        limits=limits,
        **more,
    )


async def test_man_runs_exactly_the_fixed_argv(stand_in: StandIn) -> None:
    result = await stand_in.exec().man("ls")
    assert result.ok and result.reason is Reason.OK
    argv = stand_in.record["argv"]
    assert argv[-3:] == ["man", "--", "ls"]
    pager = {"MANPAGER": "cat", "PAGER": "cat", "MANWIDTH": "80"}
    assert argv == expected(stand_in, ["man", "--", "ls"], readonly_exec.DOC_LIMITS, env=pager)


async def test_man_puts_the_section_before_the_name(stand_in: StandIn) -> None:
    await stand_in.exec().man("printf", "3")
    assert stand_in.record["argv"][-4:] == ["man", "--", "3", "printf"]


async def test_man_uses_the_session_language_when_it_is_a_plain_name(
    stand_in: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    await stand_in.exec().man("ls")
    argv = stand_in.record["argv"]
    assert argv[argv.index("LANG") + 1] == "de_DE.UTF-8"
    monkeypatch.setenv("LC_ALL", "$(reboot)")
    await stand_in.exec().man("ls")
    argv = stand_in.record["argv"]
    assert "$(reboot)" not in argv
    assert argv[argv.index("LC_ALL") + 1] == "de_DE.UTF-8"


async def test_tldr_runs_exactly_the_fixed_argv_with_the_page_cache(
    stand_in: StandIn, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / ".cache" / "tealdeer").mkdir(parents=True)
    result = await stand_in.exec(home).tldr("tar")
    assert result.ok
    cache = (str(home.resolve() / ".cache" / "tealdeer"), "/home/sandbox/.cache/tealdeer")
    assert stand_in.record["argv"] == expected(
        stand_in, ["tldr", "--", "tar"], readonly_exec.DOC_LIMITS, ro_binds=[cache]
    )


async def test_probe_runs_exactly_the_exe_and_flag_in_english(
    stand_in: StandIn, an_exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    result = await stand_in.exec().probe_version(an_exe, "--version")
    assert result.ok
    assert stand_in.record["argv"] == expected(
        stand_in,
        [str(an_exe), "--version"],
        readonly_exec.PROBE_LIMITS,
        ro_binds=[(str(an_exe), str(an_exe))],
    )


HOSTILE_NAMES = [
    "-rf",
    "--help",
    "a/b",
    "/etc/passwd",
    "..",
    "../x",
    "two words",
    "ls\n",
    "ls\nrm",
    "a" * 65,
    "",
    "ls;id",
    "$(id)",
    "ls\0",
    "é",
]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
async def test_hostile_names_are_refused_before_anything_runs(
    stand_in: StandIn, spawn_spy: list[Any], name: str
) -> None:
    runner = stand_in.exec()
    for result in (await runner.man(name), await runner.tldr(name), await runner.man(name, "1")):
        assert (result.ok, result.text, result.reason) == (False, "", Reason.INVALID)
    assert spawn_spy == []
    assert not stand_in.ran


@pytest.mark.parametrize("section", ["0", "9", "1p", "-k", "", "1 2", "--"])
async def test_unknown_man_sections_are_refused(
    stand_in: StandIn, spawn_spy: list[Any], section: str
) -> None:
    assert (await stand_in.exec().man("ls", section)).reason is Reason.INVALID
    assert spawn_spy == []


async def test_the_longest_allowed_name_is_accepted(stand_in: StandIn) -> None:
    assert (await stand_in.exec().man("a" * 64)).ok


@pytest.mark.parametrize("missing", ["bwrap", "prlimit"])
async def test_without_the_sandbox_tools_nothing_is_executed(
    stand_in: StandIn, spawn_spy: list[Any], an_exe: Path, missing: str
) -> None:
    (stand_in.directory / missing).unlink()
    runner = stand_in.exec()
    results = [
        await runner.man("ls"),
        await runner.tldr("ls"),
        await runner.probe_version(an_exe, "--version"),
    ]
    assert [r.reason for r in results] == [Reason.UNAVAILABLE] * 3
    assert not any(r.ok or r.text for r in results)
    assert spawn_spy == []
    assert await runner.status() == SandboxStatus(False, f"{missing} is not installed")


async def test_a_sandbox_tool_that_is_not_executable_counts_as_missing(
    stand_in: StandIn, spawn_spy: list[Any]
) -> None:
    stand_in.bwrap.chmod(0o644)
    assert (await stand_in.exec().man("ls")).reason is Reason.UNAVAILABLE
    assert spawn_spy == []


async def test_when_the_sandbox_cannot_be_set_up_the_tool_is_never_run(
    stand_in: StandIn, spawn_spy: list[Any]
) -> None:
    stand_in.mode("broken")
    runner = stand_in.exec()
    assert (await runner.man("ls")).reason is Reason.UNAVAILABLE
    assert (await runner.tldr("ls")).reason is Reason.UNAVAILABLE
    assert await runner.status() == SandboxStatus(False, "the sandbox could not be set up")
    assert not stand_in.ran  # only the self-check was started, never a tool
    assert len(spawn_spy) == 1 and spawn_spy[0][-1] == "--version"


async def test_the_sandbox_is_checked_once_per_instance(stand_in: StandIn) -> None:
    runner = stand_in.exec()
    assert await runner.status() == SandboxStatus(True, "ok")
    await runner.man("ls")
    await runner.man("cp")
    assert stand_in.checks == 1


async def test_a_missing_tool_is_reported_without_running_anything(
    stand_in: StandIn, spawn_spy: list[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(readonly_exec, "TOOL_DIRS", (str(tmp_path / "empty"),))
    runner = stand_in.exec()
    assert (await runner.man("ls")).reason is Reason.NOT_INSTALLED
    assert (await runner.tldr("ls")).reason is Reason.NOT_INSTALLED
    assert spawn_spy == []


async def test_the_child_gets_no_stdin_no_environment_and_its_own_session(
    stand_in: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)  # our own stdin is now a pipe the child could inherit
    try:
        await stand_in.exec().man("ls")
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)
    record = stand_in.record
    assert record["stdin"] == "/dev/null"
    assert record["env"] == ""
    assert record["leads_own_session"] is True
    assert record["stderr"] == "/dev/null"
    assert record["stdout"].startswith("pipe:")


async def test_probe_output_includes_stderr_and_man_output_does_not(
    stand_in: StandIn, an_exe: Path
) -> None:
    stand_in.mode("both")
    runner = stand_in.exec()
    assert (await runner.probe_version(an_exe, "-version")).text == "to-stderr\nto-stdout\n"
    assert stand_in.record["stderr"] == stand_in.record["stdout"]
    assert (await runner.man("ls")).text == "to-stdout\n"


async def test_output_is_cleaned_as_untrusted_text(stand_in: StandIn) -> None:
    result = await stand_in.exec().man("ls")
    assert result.text == "page [31mtext here\n"
    assert result.elapsed_ms >= 0


async def test_a_failing_tool_gives_no_text(stand_in: StandIn) -> None:
    stand_in.mode("fail")
    result = await stand_in.exec().man("nosuchpage")
    assert (result.ok, result.text, result.reason) == (False, "", Reason.FAILED)


async def test_output_is_capped_and_the_writer_is_killed(stand_in: StandIn, an_exe: Path) -> None:
    stand_in.mode("flood")
    result = await stand_in.exec().probe_version(an_exe, "--version")
    assert result.reason is Reason.TRUNCATED and result.ok
    assert len(result.text) == readonly_exec.PROBE_OUTPUT_BYTES == 4096
    assert wait_dead(stand_in.record["pid"])


async def test_a_cut_in_the_middle_of_a_character_leaves_no_broken_character(
    stand_in: StandIn, an_exe: Path
) -> None:
    stand_in.mode("multibyte")
    result = await stand_in.exec().probe_version(an_exe, "--version")
    assert result.reason is Reason.TRUNCATED
    assert result.text == "a" + "\u00e9" * 2047  # 4095 bytes; the half character is dropped


@pytest.mark.parametrize("mode", ["hang", "silent_hang"])
async def test_the_wall_clock_kills_the_whole_process_group(
    stand_in: StandIn, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(readonly_exec, "DOC_SECONDS", 0.5)
    stand_in.mode(mode)
    started = time.monotonic()
    result = await stand_in.exec().man("ls")
    assert time.monotonic() - started < 5
    assert (result.ok, result.text, result.reason) == (False, "", Reason.TIMEOUT)
    assert result.elapsed_ms >= 500
    record = stand_in.record
    assert wait_dead(record["pid"])
    assert wait_dead(record["child"]), "a child of the tool outlived the timeout"


async def test_cancelling_a_call_kills_the_process(stand_in: StandIn) -> None:
    stand_in.mode("hang")
    task = asyncio.ensure_future(stand_in.exec().man("ls"))
    while not stand_in.ran:
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wait_dead(stand_in.record["pid"])
    assert wait_dead(stand_in.record["child"])


async def test_version_flags_are_a_closed_list(
    stand_in: StandIn, spawn_spy: list[Any], an_exe: Path
) -> None:
    runner = stand_in.exec()
    for flag in ("--help", "-v", "--version --exec", "", "-e", "--version\n", "version"):
        assert (await runner.probe_version(an_exe, flag)).reason is Reason.INVALID
    assert spawn_spy == []
    for flag in ("--version", "-version", "-V"):
        assert (await runner.probe_version(an_exe, flag)).ok
        assert stand_in.record["argv"][-2:] == [str(an_exe), flag]


async def test_probe_targets_that_are_not_a_real_executable_file_are_refused(
    stand_in: StandIn, spawn_spy: list[Any], an_exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = an_exe.parent
    link = folder / "link"
    link.symlink_to(an_exe)
    linked_folder = folder.parent / "linked-apps"
    linked_folder.symlink_to(folder)
    plain = folder / "notes.txt"
    plain.write_text("not a program")
    plain.chmod(0o644)
    odd = folder / "odd\nname"
    odd.write_text("#!/bin/sh\n")
    odd.chmod(0o755)
    monkeypatch.chdir(folder)
    targets = [
        link,  # a symlink to a program
        linked_folder / "tool",  # a program reached through a symlinked folder
        folder,  # a directory
        Path("tool"),  # relative
        Path("./tool"),
        folder / ".." / "apps" / "tool",  # not normalised
        plain,  # not executable
        folder / "missing",
        odd,  # control character in the path
        Path("/proc/self/exe"),
        Path("/dev/null"),
    ]
    runner = stand_in.exec()
    for target in targets:
        result = await runner.probe_version(target, "--version")
        assert result.reason is Reason.INVALID, target
    assert spawn_spy == []
    assert stat.S_ISREG(an_exe.stat().st_mode)
    assert (await runner.probe_version(an_exe, "--version")).ok


async def test_log_lines_carry_only_the_verb_a_code_sizes_and_timings(
    stand_in: StandIn, an_exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(log, "event", lambda name, **fields: lines.append((name, fields)))
    runner = stand_in.exec()
    await runner.man("secretpage", "1")
    await runner.man("-bad-secretpage")
    await runner.probe_version(an_exe, "--version")
    events = [fields for name, fields in lines if name == "readonly_exec"]
    assert [e["verb"] for e in events] == ["man", "man", "probe_version"]
    assert [e["reason"] for e in events] == ["ok", "invalid", "ok"]
    for name, fields in lines:
        assert name in ("readonly_exec", "sandbox_status")
        assert set(fields) <= {"verb", "reason", "chars", "elapsed_ms", "available"}
        text = json.dumps(fields)
        assert "secretpage" not in text and str(an_exe) not in text and "page" not in text


# ---------------------------------------------------------------- the real sandbox


@pytest.fixture
def real() -> ReadOnlyExec:
    runner = ReadOnlyExec()
    status = asyncio.run(runner.status())
    if not status.available:
        pytest.skip(f"no usable sandbox here: {status.reason}")
    return runner


def script(tmp_path: Path, body: str) -> Path:
    exe = tmp_path.resolve() / "probe-me"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(0o755)
    return exe


async def test_real_probe_sees_a_bare_read_only_system(
    real: ReadOnlyExec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    marker = tmp_path / "private-note"
    marker.write_text("mine")
    exe = script(
        tmp_path,
        "echo version 1.2.3\n"
        "echo env:$(env | cut -d= -f1 | sort | tr '\\n' ' ')\n"
        "echo home:$HOME:$(ls -A $HOME | wc -l)\n"
        f"test -e {marker} && echo SEES-NEIGHBOUR\n"
        "test -e /run && echo SEES-RUN\n"
        "test -e /etc/passwd && echo SEES-PASSWD\n"
        "echo x 2>/dev/null >/usr/waddle-test && echo WROTE-USR\n"
        'echo x 2>/dev/null >>"$0" && echo WROTE-SELF\n'
        "echo net:$(grep -c : /proc/net/dev)\n"
        "echo stdin:$(readlink /proc/self/fd/0)\n"
        "echo pid:$$\n"
        "echo x >/tmp/waddle-sandbox-scratch && echo tmp-ok\n",
    )
    result = await real.probe_version(exe, "--version")
    assert result.reason is Reason.OK, result
    lines = result.text.splitlines()
    assert lines[0] == "version 1.2.3"
    assert "env:HOME LANG LC_ALL PATH PWD TERM" in lines
    assert "home:/home/sandbox:0" in lines
    assert "net:1" in lines  # loopback only
    assert "stdin:/dev/null" in lines
    assert "tmp-ok" in lines
    assert not [line for line in lines if line.startswith(("SEES", "WROTE"))]
    pid = int(next(line for line in lines if line.startswith("pid:"))[4:])
    assert pid < 10  # its own process namespace
    assert not Path("/tmp/waddle-sandbox-scratch").exists()
    assert exe.read_text().endswith("tmp-ok\n")


async def test_real_sandbox_has_no_capabilities_and_cannot_make_the_system_writable(
    real: ReadOnlyExec, tmp_path: Path
) -> None:
    # Matters when Waddle itself runs as root: bwrap then keeps capabilities unless told to
    # drop them, and a tool could remount /usr writable and change the real system.
    exe = script(
        tmp_path,
        "grep CapEff /proc/self/status\n"
        "mount -o remount,rw,bind /usr 2>/dev/null && echo REMOUNTED\n"
        "echo x 2>/dev/null >/usr/waddle-test && echo WROTE-USR\n"
        "echo done\n",
    )
    result = await real.probe_version(exe, "--version")
    try:
        lines = result.text.split()
        assert lines == ["CapEff:", "0000000000000000", "done"]
    finally:
        Path("/usr/waddle-test").unlink(missing_ok=True)


async def test_real_sandbox_is_writable_only_in_its_small_scratch_places(
    real: ReadOnlyExec, tmp_path: Path
) -> None:
    # The sandbox root and /dev are memory-backed too; left writable they would let a tool
    # fill half the machine's memory before the wall clock stops it.
    exe = script(
        tmp_path,
        "for d in / /dev /dev/shm /dev/pts /etc /var /home /opt /proc /tmp /home/sandbox; do\n"
        '  echo x 2>/dev/null >"$d/w" && echo "writable:$d"\n'
        "done\n"
        "for d in /dev/shm /tmp /home/sandbox; do\n"
        "  n=0\n"
        "  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do\n"
        '    head -c 1000000 /dev/zero 2>/dev/null >"$d/f$i" && n=$((n+1))\n'
        "  done\n"
        '  echo "held:$d:$n"\n'
        "done\n"
        "echo x >/dev/null && echo null-ok\n",
    )
    result = await real.probe_version(exe, "--version")
    assert result.reason is Reason.OK, result
    lines = result.text.splitlines()
    assert [line for line in lines if line.startswith("writable:")] == [
        "writable:/dev/shm",
        "writable:/tmp",
        "writable:/home/sandbox",
    ]
    assert [line for line in lines if line.startswith("held:")] == [
        "held:/dev/shm:8",
        "held:/tmp:8",
        "held:/home/sandbox:8",
    ]
    assert "null-ok" in lines


async def test_real_probe_times_out_and_leaves_nothing_running(
    real: ReadOnlyExec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readonly_exec, "PROBE_SECONDS", 0.5)
    result = await real.probe_version(script(tmp_path, "sleep 61.803 &\nsleep 61.803\n"), "-V")
    assert (result.ok, result.text, result.reason) == (False, "", Reason.TIMEOUT)

    def sleepers() -> list[str]:
        found = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal():
                try:
                    if b"61.803" in (entry / "cmdline").read_bytes():
                        found.append(entry.name)
                except OSError:
                    continue
        return found

    deadline = time.monotonic() + 3
    while sleepers() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sleepers() == []


async def test_real_probe_output_is_capped(real: ReadOnlyExec, tmp_path: Path) -> None:
    result = await real.probe_version(script(tmp_path, "yes version\n"), "--version")
    assert result.reason is Reason.TRUNCATED
    assert len(result.text) == 4096


async def test_real_limits_stop_large_files(real: ReadOnlyExec, tmp_path: Path) -> None:
    exe = script(tmp_path, "head -c 3000000 /dev/zero >/tmp/big\necho rc:$?\n")
    result = await real.probe_version(exe, "--version")
    assert "rc:153" in result.text  # head was stopped by the file size limit (128 + SIGXFSZ)


async def test_real_probe_of_a_system_program(real: ReadOnlyExec) -> None:
    bwrap = readonly_exec.find_tool(readonly_exec.BWRAP_CANDIDATES)
    assert bwrap is not None
    result = await real.probe_version(bwrap.resolve(), "--version")
    assert result.ok and result.text.startswith("bubblewrap ")


async def test_real_man_page(real: ReadOnlyExec) -> None:
    man = Path("/usr/bin/man")
    if not man.is_file() or man.read_bytes()[:4] != b"\x7fELF":
        pytest.skip("no real man here")
    if not list(Path("/usr/share/man/man1").glob("man.1*")):
        pytest.skip("no man pages installed")
    result = await real.man("man", "1")
    assert result.ok and "NAME" in result.text
    assert (await real.man("no-such-page-waddle")).reason is Reason.FAILED


async def test_sandbox_status_reports_this_machine() -> None:
    status = await readonly_exec.sandbox_status()
    tools = [
        readonly_exec.find_tool(readonly_exec.BWRAP_CANDIDATES),
        readonly_exec.find_tool(readonly_exec.PRLIMIT_CANDIDATES),
    ]
    if None in tools:
        assert not status.available and status.reason.endswith("is not installed")
    else:
        assert status.reason in ("ok", "the sandbox could not be set up")
        assert status.available == (status.reason == "ok")


def test_the_module_keeps_no_way_to_run_an_arbitrary_command() -> None:
    public = {name for name in vars(ReadOnlyExec) if not name.startswith("_")}
    assert public == {"status", "man", "tldr", "probe_version"}
    assert Path(readonly_exec.__file__).name == "readonly_exec.py"
