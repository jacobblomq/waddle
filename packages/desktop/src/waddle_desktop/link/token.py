"""Waddle's link token: the second factor on the brain socket (ARCHITECTURE §5.2).

The token is Waddle's own file. It is created once, readable only by the user, and is never
logged or printed: callers get the value and must keep it out of logs too.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Final

from waddle_core.link import TOKEN_BYTES

_TOKEN: Final = re.compile(rf"[0-9a-f]{{{TOKEN_BYTES * 2}}}")


class TokenError(Exception):
    """The token file cannot be created or trusted. ``code`` is safe to log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def read_token(path: Path) -> str | None:
    """The token in ``path``, or ``None`` unless it is a private regular file of ours.

    Refuses symlinks, files owned by someone else, files readable by group or others, and
    anything that is not exactly 64 lowercase hex characters (one trailing newline allowed).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        if info.st_mode & 0o077:
            return None
        raw = os.read(fd, TOKEN_BYTES * 2 + 2)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        text = raw.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        return None
    return text if _TOKEN.fullmatch(text) else None


def ensure_token(path: Path) -> str:
    """Return the token in ``path``, creating it (mode 0600, parent 0700) if it is missing.

    Creation is atomic: the token is written to a private temporary file and hard-linked into
    place, so a reader never sees a half-written file and an existing file is never replaced.
    Raises ``TokenError`` if the file exists but cannot be trusted, or cannot be created.
    """
    token = read_token(path)
    if token is not None:
        return token
    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not os.path.lexists(path):
            parent.chmod(0o700)
            _create(path, secrets.token_hex(TOKEN_BYTES))
    except OSError:
        raise TokenError("token_unwritable") from None
    token = read_token(path)  # ours, or the one a concurrent start just created
    if token is None:
        raise TokenError("bad_token_file")
    return token


def _create(path: Path, token: str) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            os.write(fd, token.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        with contextlib.suppress(FileExistsError):  # lost a race: keep the other one
            os.link(temp, path)
    finally:
        temp.unlink()
