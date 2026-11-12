"""``identify()``: steps 1-2 of the version waterfall, and its cache (ARCHITECTURE §4.6).

The cache lives in Waddle's own SQLite file (mode 0600). A key contains what changes when
the application is updated (the Flatpak commit, the snap revision, or the executable's
size and mtime), so an update invalidates the entry by itself and a hit needs no metadata
read at all.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import posixpath
import re
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from waddle_desktop import log
from waddle_desktop.identity.packaging import (
    FsReader,
    PackageInfo,
    Packaging,
    detect_packaging,
    flatpak_app_id,
    flatpak_commit,
    snap_name,
    snap_revision,
)
from waddle_desktop.identity.resolve import ResolvedBinary, identity_name, resolve_binary
from waddle_desktop.identity.snapshot import ProcessSnapshot, ProcReader
from waddle_desktop.identity.version import (
    Channel,
    Version,
    detect_channel,
    detect_edition,
    parse_version,
)

_MAX_TEXT: Final = 4096
_MAX_ROWS: Final = 2000
_FORMAT: Final = 1


@dataclass(frozen=True)
class AppIdentity:
    app_id: str
    name: str
    exe: str
    packaging: Packaging
    package_id: str
    version: Version | None
    version_raw: str
    channel: Channel
    edition: str | None
    locale: None  # filled in by a later phase
    source: str
    confidence: float

    def to_json(self) -> str:
        version = self.version
        return json.dumps(
            {
                "format": _FORMAT,
                "app_id": self.app_id,
                "name": self.name,
                "exe": self.exe,
                "packaging": self.packaging.value,
                "package_id": self.package_id,
                "version": None
                if version is None
                else {
                    "major": version.major,
                    "minor": version.minor,
                    "patch": version.patch,
                    "pre": version.pre,
                },
                "version_raw": self.version_raw,
                "channel": self.channel.value,
                "edition": self.edition,
                "locale": None,
                "source": self.source,
                "confidence": self.confidence,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> AppIdentity:
        """Inverse of ``to_json``. Raises ValueError on anything that is not exactly that."""
        try:
            data = json.loads(text)
        except RecursionError as error:
            raise ValueError("nested too deeply") from error
        if not isinstance(data, dict) or data.get("format") != _FORMAT:
            raise ValueError("not an identity record")

        def string(key: str) -> str:
            value = data.get(key)
            if not isinstance(value, str) or len(value) > _MAX_TEXT:
                raise ValueError(key)
            return value

        def integer(value: object) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("version")
            return value

        raw_version = data.get("version")
        version: Version | None = None
        if raw_version is not None:
            if not isinstance(raw_version, dict) or not isinstance(raw_version.get("pre"), str):
                raise ValueError("version")
            patch = raw_version.get("patch")
            version = Version(
                integer(raw_version.get("major")),
                integer(raw_version.get("minor")),
                None if patch is None else integer(patch),
                raw_version["pre"],
            )
        edition = data.get("edition")
        if edition is not None and (not isinstance(edition, str) or len(edition) > _MAX_TEXT):
            raise ValueError("edition")
        confidence = data.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise ValueError("confidence")
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence")
        if data.get("locale") is not None:
            raise ValueError("locale")
        return cls(
            app_id=string("app_id"),
            name=string("name"),
            exe=string("exe"),
            packaging=Packaging(string("packaging")),
            package_id=string("package_id"),
            version=version,
            version_raw=string("version_raw"),
            channel=Channel(string("channel")),
            edition=edition,
            locale=None,
            source=string("source"),
            confidence=float(confidence),
        )


UNKNOWN_IDENTITY: Final = AppIdentity(
    app_id="",
    name="",
    exe="",
    packaging=Packaging.UNKNOWN,
    package_id="",
    version=None,
    version_raw="",
    channel=Channel.STABLE,
    edition=None,
    locale=None,
    source="unknown",
    confidence=0.0,
)


def cache_key(
    app_id: str,
    exe: str,
    *,
    package_version: str | None = None,
    exe_stat: tuple[int, int] | None = None,
) -> str | None:
    """The cache key, or None when nothing is known that would change on an update."""
    if package_version:
        token = f"pkg:{package_version}"
    elif exe_stat is not None:
        token = f"stat:{exe_stat[0]}:{exe_stat[1]}"
    else:
        return None
    return json.dumps([app_id, exe, token], separators=(",", ":"))


class IdentityCache:
    """A small key -> AppIdentity table. Safe to call from any thread; never raises."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.close(os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600))
            path.chmod(0o600)
            db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            db.execute(
                "CREATE TABLE IF NOT EXISTS identity("
                "key TEXT PRIMARY KEY, json TEXT, created_ms INT)"
            )
            self._db = db
        except (OSError, sqlite3.Error):
            # An unusable cache file only costs speed: every lookup is then a cold one.
            log.warning("identity.cache_unavailable")

    def get(self, key: str) -> AppIdentity | None:
        with self._lock:
            if self._db is None:
                return None
            try:
                row = self._db.execute("SELECT json FROM identity WHERE key = ?", (key,)).fetchone()
                if row is None:
                    return None
                try:
                    if not isinstance(row[0], str):
                        raise ValueError("json column is not text")
                    return AppIdentity.from_json(row[0])
                except ValueError:
                    self._db.execute("DELETE FROM identity WHERE key = ?", (key,))
                    log.warning("identity.cache_row_dropped")
                    return None
            except (sqlite3.Error, ValueError):  # ValueError: a key SQLite cannot encode
                return None

    def put(self, key: str, identity: AppIdentity) -> None:
        with self._lock:
            if self._db is None:
                return
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO identity(key, json, created_ms) VALUES (?, ?, ?)",
                    (key, identity.to_json(), time.time_ns() // 1_000_000),
                )
                self._db.execute(
                    "DELETE FROM identity WHERE key NOT IN "
                    "(SELECT key FROM identity ORDER BY created_ms DESC, rowid DESC LIMIT ?)",
                    (_MAX_ROWS,),
                )
            except (sqlite3.Error, ValueError):
                return

    def forget(self) -> None:
        """Drop every cached identity."""
        with self._lock:
            if self._db is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._db.execute("DELETE FROM identity")

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._db.close()
                self._db = None


# --------------------------------------------------------------------------- identify

_DESKTOP_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_UNSAFE: Final = re.compile(r"[^a-z0-9._+-]+")
_CONFIDENCE: Final = {
    "flatpak-metadata": 0.95,
    "dpkg": 0.95,
    "snap-yaml": 0.95,
    "snap-env": 0.9,
    "version-file": 0.85,
    "appimage-name": 0.6,
    "dir-name": 0.6,
}
_FLATPAK_BRANCH_CHANNELS: Final = {
    "beta": Channel.BETA,
    "nightly": Channel.NIGHTLY,
    "daily": Channel.NIGHTLY,
    "master": Channel.NIGHTLY,
    "devel": Channel.NIGHTLY,
}


def _desktop_id(desktop_file: object) -> str:
    """KWin's desktopFileName as an id: no directory, no ".desktop"; "" when not id-shaped."""
    if not isinstance(desktop_file, str) or len(desktop_file) > _MAX_TEXT:
        return ""
    name = posixpath.basename(desktop_file)
    if name.endswith(".desktop"):
        name = name[: -len(".desktop")]
    return name if _DESKTOP_ID.fullmatch(name) else ""


def _window_class_id(window_class: object) -> str:
    text = window_class[:128].lower() if isinstance(window_class, str) else ""
    return _UNSAFE.sub("-", text).strip("-.")[:64]


def _clean(text: str) -> str:
    """Text taken from a process or a path, fit to show, send and store: nothing that does
    not print (control characters, bidi overrides, lone surrogates). It stays untrusted."""
    return "".join(ch for ch in text[:_MAX_TEXT] if ch.isprintable())


def _update_token(
    binary: ResolvedBinary,
    chain: Sequence[ProcessSnapshot],
    fs: FsReader,
    *,
    sandboxed: bool,
) -> tuple[str | None, tuple[int, int] | None]:
    """(package version token, exe stat): whichever is known without reading metadata."""
    commit = flatpak_commit(chain)
    if commit:
        return f"flatpak:{commit}", None
    revision = snap_revision(chain) if snap_name(chain) else None
    if revision:
        return f"snap:{revision}", None
    path = binary.identity_path
    # A sandboxed exe path means nothing on the host, so only the kernel's own view of the
    # running executable (taken through /proc) can be used for it.
    stat = fs.stat(path) if path.startswith("/") and not sandboxed else None
    if stat is None:
        owner = next((s for s in chain if s.exe == binary.exe), chain[0])
        stat = owner.exe_stat
    return None, stat


def _build(
    binary: ResolvedBinary,
    info: PackageInfo,
    app_id: str,
    hints: Sequence[str],
) -> AppIdentity:
    raw = info.version or ""
    version = parse_version(raw) if raw else None
    channel = detect_channel(raw) if raw else Channel.STABLE
    if channel is Channel.STABLE and info.kind is Packaging.FLATPAK and info.channel:
        channel = _FLATPAK_BRANCH_CHANNELS.get(info.channel.lower(), Channel.STABLE)
    name = identity_name(binary) or app_id.rsplit(".", 1)[-1].lower()
    if version is not None:
        confidence = _CONFIDENCE.get(info.source, 0.5)
    else:
        confidence = 0.1 if info.kind is Packaging.UNKNOWN else 0.3
    return AppIdentity(
        app_id=_clean(app_id),
        name=_clean(name),
        exe=_clean(binary.identity_path),
        packaging=info.kind,
        package_id=_clean(info.package_id),
        version=version,
        version_raw=raw if version is not None else "",
        channel=channel,
        edition=detect_edition(app_id, hints),
        locale=None,
        source=info.source if version is not None else "unknown",
        confidence=confidence,
    )


def _identify(
    pid: int,
    reader: ProcReader,
    fs: FsReader,
    home: Path,
    cache: IdentityCache | None,
    window_class: str,
    desktop_file: str,
) -> AppIdentity:
    chain = reader.chain(pid)
    desktop_id = _desktop_id(desktop_file)
    class_id = _window_class_id(window_class)
    if not chain:
        return replace(UNKNOWN_IDENTITY, app_id=desktop_id or class_id)
    binary = resolve_binary(chain)
    flatpak_id = flatpak_app_id(chain)
    snap = snap_name(chain)
    app_id = flatpak_id or snap or desktop_id or identity_name(binary) or class_id

    sandboxed = flatpak_id is not None or "flatpak" in binary.wrapper_kinds
    package_version, exe_stat = _update_token(binary, chain, fs, sandboxed=sandboxed)
    key = cache_key(
        app_id, binary.identity_path, package_version=package_version, exe_stat=exe_stat
    )
    if cache is not None and key is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit

    info = detect_packaging(binary, chain, fs, home=home)
    hints = [
        *binary.identity_path.split("/")[-12:],
        info.package_id,
        desktop_id,
        class_id,
    ]
    identity = _build(binary, info, app_id, hints)
    if cache is not None and key is not None:
        cache.put(key, identity)
    return identity


def identify(
    pid: int,
    reader: ProcReader,
    fs: FsReader,
    home: Path,
    cache: IdentityCache | None,
    *,
    window_class: str = "",
    desktop_file: str = "",
) -> AppIdentity:
    """Which app, which packaging, which version, from side-effect-free reads only.

    Blocking (file reads): call it off the UI thread. Never raises; an unknown process
    yields ``UNKNOWN_IDENTITY`` (with the window's own id when it has one).
    """
    try:
        return _identify(pid, reader, fs, home, cache, window_class, desktop_file)
    except Exception:  # everything read here is untrusted: a parser bug must not escape
        log.warning("identity.failed")
        return UNKNOWN_IDENTITY
