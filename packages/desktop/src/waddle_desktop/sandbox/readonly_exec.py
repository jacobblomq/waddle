"""Everything Waddle runs to *observe* (ARCHITECTURE §4.12, invariant I1, D-043).

Three verbs and nothing else: read a man page, read a tldr page, ask an installed program for
its version. There is no generic "run this". Each verb is a fixed argv template, started
without a shell inside the one sandbox profile (``profiles.sandbox_argv``): no network, no
home, no display, no session bus, a read-only view of the system, resource limits, capped
output and a wall-clock kill of the whole process group.

It fails closed. If ``bwrap`` or ``prlimit`` is missing, or the sandbox cannot be set up
(user namespaces disabled), every verb returns ``Reason.UNAVAILABLE`` and nothing is run.

Output is untrusted data: it is cleaned and capped here, and callers must only ever place it
in a labelled data block. Log lines carry the verb, a reason code, sizes and timings only.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import re
import signal
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

from waddle_core.tools import NAME_PATTERN
from waddle_core.untrusted import clean
from waddle_desktop import log
from waddle_desktop.sandbox.profiles import (
    Limits,
    probe_binds,
    safe_locale,
    sandbox_argv,
    tldr_cache_binds,
)

# Resolved once from these fixed places, never from the caller's PATH.
BWRAP_CANDIDATES: Final = ("/usr/bin/bwrap", "/bin/bwrap")
PRLIMIT_CANDIDATES: Final = ("/usr/bin/prlimit", "/bin/prlimit")
TOOL_DIRS: Final = ("/usr/local/bin", "/usr/bin", "/bin")

MAN_SECTIONS: Final = frozenset("12345678")
# "--version" is the GNU convention, "-version" the Java/ffmpeg one, "-V" the short form.
# Left out on purpose: "-v" (often "verbose": the program would start normally) and "--help"
# (not a version, and long).
VERSION_FLAGS: Final = frozenset({"--version", "-version", "-V"})

MAN_OUTPUT_BYTES: Final = 24 * 1024
TLDR_OUTPUT_BYTES: Final = 8 * 1024
PROBE_OUTPUT_BYTES: Final = 4 * 1024
DOC_SECONDS: Final = 5.0
PROBE_SECONDS: Final = 3.0
MAX_EXE_PATH_CHARS: Final = 1024

_MIB: Final = 1024 * 1024
# UNVERIFIED (spike S3.5): limits are sized for man and for small native --version calls.
# Programs that reserve a huge address space at start (Electron, JVM) fail the probe and
# identity falls through to its next step.
DOC_LIMITS: Final = Limits(
    address_space_bytes=1024 * _MIB, cpu_seconds=5, file_size_bytes=_MIB, processes=64
)
PROBE_LIMITS: Final = Limits(
    address_space_bytes=4096 * _MIB, cpu_seconds=3, file_size_bytes=_MIB, processes=64
)
_MAN_ENV: Final = {"MANPAGER": "cat", "PAGER": "cat", "MANWIDTH": "80"}
_NAME: Final = re.compile(NAME_PATTERN)


class Reason(StrEnum):
    OK = "ok"
    TRUNCATED = "truncated"  # the output cap was reached; the start of the text is returned
    INVALID = "invalid"  # the argument was refused; nothing was run
    UNAVAILABLE = "unavailable"  # no working sandbox; nothing was run
    NOT_INSTALLED = "not_installed"  # the tool itself (man, tldr) is not on this system
    FAILED = "failed"  # the tool ran and exited with an error
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ExecResult:
    """What a verb produced. ``text`` is untrusted data, already cleaned and capped."""

    ok: bool
    text: str
    reason: Reason
    elapsed_ms: int


@dataclass(frozen=True)
class SandboxStatus:
    """Whether observation commands can run, for ``waddle doctor``."""

    available: bool
    reason: str


def find_tool(candidates: Sequence[str]) -> Path | None:
    """The first candidate that is an executable regular file."""
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


class ReadOnlyExec:
    """The sandboxed verbs. One instance is shared; it remembers whether the sandbox works.

    ``bwrap`` and ``prlimit`` default to the fixed system locations and are injectable for
    tests. ``home`` is only used to find tldr page caches. ``root`` is where the profile looks
    for system directories.
    """

    def __init__(
        self,
        *,
        bwrap: Path | None = None,
        prlimit: Path | None = None,
        home: Path | None = None,
        root: Path = Path("/"),
    ) -> None:
        self._bwrap = find_tool(BWRAP_CANDIDATES if bwrap is None else (str(bwrap),))
        self._prlimit = find_tool(PRLIMIT_CANDIDATES if prlimit is None else (str(prlimit),))
        self._home = Path.home() if home is None else home
        self._root = root
        self._status: SandboxStatus | None = None

    async def status(self) -> SandboxStatus:
        """Check once that the full profile can start a process, then remember the answer."""
        if self._status is None:
            self._status = await self._check()
            log.event("sandbox_status", available=self._status.available)
        return self._status

    async def man(self, name: str, section: str | None = None) -> ExecResult:
        """The formatted man page ``name`` in the user's language, capped."""
        if not _NAME.fullmatch(name) or (section is not None and section not in MAN_SECTIONS):
            return _refused("man", Reason.INVALID)
        if find_tool([f"{d}/man" for d in TOOL_DIRS]) is None:
            return _refused("man", Reason.NOT_INSTALLED)
        command = ["man", "--", *([section] if section else []), name]
        return await self._run(
            "man",
            command,
            limits=DOC_LIMITS,
            seconds=DOC_SECONDS,
            cap=MAN_OUTPUT_BYTES,
            env=_MAN_ENV,
            locale=_user_locale(),
        )

    async def tldr(self, name: str) -> ExecResult:
        """The tldr page ``name``, if a tldr client and its page cache are installed.

        Some clients try to refresh their cache over the network. The sandbox has none, so
        such a client answers from the pages it already has or fails.
        """
        if not _NAME.fullmatch(name):
            return _refused("tldr", Reason.INVALID)
        if find_tool([f"{d}/tldr" for d in TOOL_DIRS]) is None:
            return _refused("tldr", Reason.NOT_INSTALLED)
        # UNVERIFIED (spike S3.6): checked with tealdeer only; the other clients' cache
        # locations in profiles.TLDR_CACHES are from their documentation.
        return await self._run(
            "tldr",
            ["tldr", "--", name],
            limits=DOC_LIMITS,
            seconds=DOC_SECONDS,
            cap=TLDR_OUTPUT_BYTES,
            ro_binds=tldr_cache_binds(self._home),
            locale=_user_locale(),
        )

    async def probe_version(self, exe: Path, flag: str) -> ExecResult:
        """What ``exe <flag>`` prints, for an allowlisted version flag.

        ``exe`` must be the real absolute path of an executable regular file (as app identity
        reads it from ``/proc/<pid>/exe``); a path through a symlink is refused. The caller
        decides which apps may be probed. The program gets no home, no display and no bus, so
        it cannot open a window or keep settings. stderr is included because several programs
        print their version there.
        """
        real = _real_executable(exe) if flag in VERSION_FLAGS else None
        if real is None:
            return _refused("probe_version", Reason.INVALID)
        return await self._run(
            "probe_version",
            [str(real), flag],
            limits=PROBE_LIMITS,
            seconds=PROBE_SECONDS,
            cap=PROBE_OUTPUT_BYTES,
            ro_binds=probe_binds(PurePosixPath(real)),
            merge_stderr=True,
        )

    async def _check(self) -> SandboxStatus:
        if self._bwrap is None:
            return SandboxStatus(False, "bwrap is not installed")
        if self._prlimit is None:
            return SandboxStatus(False, "prlimit is not installed")
        outcome = await self._start(
            [str(self._prlimit), "--version"], limits=DOC_LIMITS, seconds=PROBE_SECONDS, cap=256
        )
        if outcome.reason is Reason.OK:
            return SandboxStatus(True, "ok")
        return SandboxStatus(False, "the sandbox could not be set up")

    async def _run(
        self,
        verb: str,
        command: Sequence[str],
        *,
        limits: Limits,
        seconds: float,
        cap: int,
        env: Mapping[str, str] | None = None,
        ro_binds: Sequence[tuple[str, str]] = (),
        merge_stderr: bool = False,
        locale: str | None = None,
    ) -> ExecResult:
        """Run one fixed command in the sandbox, or refuse if there is no sandbox."""
        if not (await self.status()).available:
            return _refused(verb, Reason.UNAVAILABLE)
        result = await self._start(
            command,
            limits=limits,
            seconds=seconds,
            cap=cap,
            env=env,
            ro_binds=ro_binds,
            merge_stderr=merge_stderr,
            locale=locale,
        )
        log.event(
            "readonly_exec",
            verb=verb,
            reason=result.reason.value,
            chars=len(result.text),
            elapsed_ms=result.elapsed_ms,
        )
        return result

    async def _start(
        self,
        command: Sequence[str],
        *,
        limits: Limits,
        seconds: float,
        cap: int,
        env: Mapping[str, str] | None = None,
        ro_binds: Sequence[tuple[str, str]] = (),
        merge_stderr: bool = False,
        locale: str | None = None,
    ) -> ExecResult:
        """Start ``command`` under the profile and collect at most ``cap`` bytes of output."""
        started = time.monotonic()
        if self._bwrap is None or self._prlimit is None:
            return ExecResult(False, "", Reason.UNAVAILABLE, 0)
        argv = sandbox_argv(
            bwrap=str(self._bwrap),
            prlimit=str(self._prlimit),
            command=command,
            limits=limits,
            locale=safe_locale(locale),
            env=env,
            ro_binds=ro_binds,
            root=self._root,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.DEVNULL,
                env={},
                cwd="/",
                start_new_session=True,
            )
        except OSError:
            return ExecResult(False, "", Reason.UNAVAILABLE, _ms_since(started))
        assert process.stdout is not None
        data, reason = b"", Reason.TIMEOUT
        try:
            async with asyncio.timeout(seconds):
                data, truncated = await _read_capped(process.stdout, cap)
                if truncated:
                    reason = Reason.TRUNCATED
                else:
                    reason = Reason.OK if await process.wait() == 0 else Reason.FAILED
        except TimeoutError:
            pass
        finally:
            if process.returncode is None:
                # Not reaped yet, so the pid still names our process group. Killing bwrap
                # takes the whole sandbox with it (--die-with-parent, its own pid namespace).
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        ok = reason in (Reason.OK, Reason.TRUNCATED)
        text = clean(_decode(data, cut=reason is Reason.TRUNCATED), cap) if ok and data else ""
        return ExecResult(ok, text, reason, _ms_since(started))


async def sandbox_status() -> SandboxStatus:
    """Whether the sandbox works on this machine, checked now (for ``waddle doctor``)."""
    return await ReadOnlyExec().status()


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    """Read to end of stream, or stop once more than ``cap`` bytes arrived (truncated)."""
    buffer = bytearray()
    while len(buffer) <= cap:
        chunk = await stream.read(cap + 1 - len(buffer))
        if not chunk:
            return bytes(buffer), False
        buffer += chunk
    return bytes(buffer[:cap]), True


def _decode(data: bytes, *, cut: bool) -> str:
    """``data`` as text. After a cut, a character split by the cap is dropped, not mangled."""
    return codecs.getincrementaldecoder("utf-8")("replace").decode(data, final=not cut)


def _real_executable(exe: Path) -> Path | None:
    """``exe`` if it is the real absolute path of an executable regular file, else None."""
    text = str(exe)
    if (
        not exe.is_absolute()
        or len(text) > MAX_EXE_PATH_CHARS
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in text)
    ):
        return None
    try:
        real = exe.resolve(strict=True)  # resolved once; this is the path that is bound
        mode = real.lstat().st_mode
    except (OSError, RuntimeError, ValueError):
        return None
    if str(real) != text or not stat.S_ISREG(mode) or not os.access(real, os.X_OK):
        return None
    return real


def _user_locale() -> str:
    """The session's message language, if it is a plain locale name."""
    return safe_locale(*(os.environ.get(name) for name in ("LC_ALL", "LC_MESSAGES", "LANG")))


def _refused(verb: str, reason: Reason) -> ExecResult:
    log.event("readonly_exec", verb=verb, reason=reason.value, chars=0, elapsed_ms=0)
    return ExecResult(False, "", reason, 0)


def _ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
