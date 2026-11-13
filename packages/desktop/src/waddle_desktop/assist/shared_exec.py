"""The one shared ``ReadOnlyExec``, and when to check the sandbox again.

``ReadOnlyExec`` remembers its self-check for good, so one slow start (a loaded machine at
login) would switch off man pages and version probes until Waddle is restarted. This wrapper
keeps one instance for everyone and, while the sandbox is unavailable, builds a fresh one for
a new self-check at most every ``RETRY_SECONDS``. It still fails closed: until a check has
passed, nothing is run.

This module starts no process itself; only ``sandbox/readonly_exec.py`` does.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol

from waddle_core import capabilities as caps
from waddle_desktop import log
from waddle_desktop.sandbox.readonly_exec import ExecResult, ReadOnlyExec, SandboxStatus

RETRY_SECONDS: Final = 300.0
_REASON_CODES: Final = {
    "bwrap is not installed": caps.SANDBOX_NO_BWRAP,
    "prlimit is not installed": caps.SANDBOX_NO_PRLIMIT,
}


class Exec(Protocol):
    """The part of ``ReadOnlyExec`` the host services use."""

    async def status(self) -> SandboxStatus: ...
    async def man(self, name: str, section: str | None = None) -> ExecResult: ...
    async def tldr(self, name: str) -> ExecResult: ...
    async def probe_version(self, exe: Path, flag: str) -> ExecResult: ...


class SharedExec:
    """Hands out the working sandbox, or None. Use from one event loop."""

    def __init__(
        self,
        factory: Callable[[], Exec] = ReadOnlyExec,
        *,
        clock: Callable[[], float] = time.monotonic,
        retry_after: float = RETRY_SECONDS,
    ):
        self._factory = factory
        self._clock = clock
        self._retry_after = retry_after
        self._exec: Exec | None = None
        self._status: SandboxStatus | None = None
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def status(self) -> SandboxStatus | None:
        """The last self-check, or None before the first one. Does not run anything."""
        return self._status

    @property
    def capability(self) -> tuple[bool, str]:
        """(available, a ``waddle_core.capabilities`` SANDBOX_* code if not)."""
        if self._status is None:
            return False, caps.SANDBOX_UNCHECKED
        if self._status.available:
            return True, ""
        return False, _REASON_CODES.get(self._status.reason, caps.SANDBOX_SETUP_FAILED)

    async def ready(self) -> Exec | None:
        """The sandbox if its self-check passed, else None (checked again after a while)."""
        async with self._lock:
            status = self._status
            if status is not None:
                if status.available:
                    return self._exec
                if self._clock() - self._checked_at < self._retry_after:
                    return None
            try:
                candidate = self._factory()
                status = await candidate.status()
            except Exception as exc:  # no sandbox is the safe reading of any surprise
                log.warning("sandbox_check_failed", kind=type(exc).__name__)
                candidate, status = None, SandboxStatus(False, "the sandbox could not be set up")
            self._status = status
            self._checked_at = self._clock()
            if status.available:
                self._exec = candidate
            return self._exec
