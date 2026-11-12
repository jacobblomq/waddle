"""Starts Waddle's own capture helper (ARCHITECTURE §4.3, D-005).

This is one of the two places in the host that may start a process, and it can start exactly
one program: the ``waddle-capture`` binary at the path Waddle's installer put it. The path is
fixed by the caller's ``WaddlePaths`` and checked here; no argument comes from a model, a
window or any other untrusted text. The helper is Waddle's own process (invariant I2).

KWin authorizes screenshots by the helper's executable path (Plasma 6.4-6.7), so it must be
started directly, never through a shell or a wrapper.
"""

from __future__ import annotations

import contextlib
import os
import socket
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


class HelperUnavailableError(RuntimeError):
    """The helper is not installed, or the file there is not something we should run."""


@dataclass
class HelperProcess:
    process: subprocess.Popen[bytes]
    sock: socket.socket  # our end; the capture client takes ownership of it

    def stop(self, timeout: float = 1.0) -> None:
        """End our own helper. Closing the socket is the request; signals are the backup."""
        with contextlib.suppress(OSError):
            self.sock.close()
        try:
            self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def check_binary(binary: Path) -> None:
    """Refuse anything that is not a private, regular, executable file of the current user."""
    if not binary.is_absolute():
        raise HelperUnavailableError("not_absolute")
    try:
        info = binary.lstat()
    except OSError:
        raise HelperUnavailableError("missing") from None
    if not stat.S_ISREG(info.st_mode):
        raise HelperUnavailableError("not_a_file")
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise HelperUnavailableError("unsafe_permissions")
    if not info.st_mode & 0o100:
        raise HelperUnavailableError("not_executable")


def start_helper(binary: Path, *, idle_timeout: int = 120) -> HelperProcess:
    check_binary(binary)
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed program, fixed arguments, no shell
            [str(binary), "--fd", str(theirs.fileno()), "--idle-timeout", str(idle_timeout)],
            pass_fds=(theirs.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=False,
        )
    except OSError:
        ours.close()
        raise HelperUnavailableError("spawn_failed") from None
    finally:
        theirs.close()
    return HelperProcess(process, ours)
