"""Which applications are installed, from their ``.desktop`` files (ARCHITECTURE §4.6).

Read-only and lazy: nothing is scanned until someone asks, and the scan only lists the XDG
application directories and reads small text files. A ``.desktop`` file is untrusted input
that any installer (or download) can place, so it is size-capped, parsed by hand for six
keys, and its ``Exec`` line is never run, never handed to a shell and never kept: only the
program's base name survives, and only if it looks like a plain file name.

Not done yet: watching the directories for changes (call ``AppInventory.invalidate`` to
rescan), localized names (``Name[de]``), and ``OnlyShowIn``/``TryExec`` filtering.
"""

from __future__ import annotations

import os
import posixpath
import re
import stat as stat_module
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from waddle_core.untrusted import clean
from waddle_desktop import log
from waddle_desktop.identity.packs import PackIndex

MAX_FILE_BYTES: Final = 64 * 1024
MAX_ENTRIES_PER_DIR: Final = 2000  # directory entries looked at, per application directory
MAX_APPS: Final = 4000
MAX_FILES: Final = 16000  # files opened in one scan, good or bad
MAX_DATA_DIRS: Final = 32
MAX_DEPTH: Final = 2  # sub-directories become an id prefix: "kde/foo.desktop" is "kde-foo"
MAX_EXEC_CHARS: Final = 1024
MAX_CATEGORIES: Final = 16

Source = Literal["user", "system", "flatpak", "snap"]

_SUFFIX: Final = ".desktop"
_GROUP: Final = "[Desktop Entry]"
_KEYS: Final = frozenset({"Type", "Name", "Exec", "Icon", "NoDisplay", "Hidden", "Categories"})
_FILE_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_ICON_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_CATEGORY: Final = re.compile(r"[A-Za-z0-9-]{1,32}")
_ENV_ASSIGNMENT: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}=")
_READ_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY
_FLATPAK_DIRS: Final = ("/var/lib/flatpak/exports/share",)
_SNAP_DIR: Final = "/var/lib/snapd/desktop"


@dataclass(frozen=True)
class InstalledApp:
    desktop_id: str  # "org.kde.kate"; from the file name, untrusted but cleaned
    name: str  # the unlocalized Name; untrusted but cleaned
    exec_name: str  # base name of the program in Exec, "" unless it is a plain file name
    icon: str  # an icon *name*; "" when the file gives a path
    categories: tuple[str, ...]
    no_display: bool  # installed, but its author keeps it out of menus
    source: Source
    pack_id: str | None  # the identity pack that knows this application


def _clean_dir(value: str) -> str | None:
    """A directory from an environment variable, if it is a plain absolute path."""
    if not value.startswith("/") or len(value) > 1024 or any(ord(c) < 32 for c in value):
        return None
    path = posixpath.normpath(value)
    return None if ".." in path.split("/") else path


def _source_of(data_dir: str, user_dir: str) -> Source:
    if "/flatpak/exports/" in data_dir + "/":
        return "flatpak"
    if data_dir == _SNAP_DIR:
        return "snap"
    return "user" if data_dir == user_dir else "system"


def application_dirs(home: Path, environ: Mapping[str, str]) -> tuple[tuple[str, Source], ...]:
    """The application directories, highest precedence first, as absolute paths.

    ``$XDG_DATA_HOME`` (default ``~/.local/share``), then ``$XDG_DATA_DIRS`` in order (default
    ``/usr/local/share:/usr/share``), then the Flatpak export directories and the snap desktop
    directory if the session did not already list them.
    """
    home_text = str(home)
    user_dir = _clean_dir(environ.get("XDG_DATA_HOME", "")) or f"{home_text}/.local/share"
    listed = [_clean_dir(part) for part in environ.get("XDG_DATA_DIRS", "").split(":")]
    data_dirs = [d for d in listed[:MAX_DATA_DIRS] if d] or ["/usr/local/share", "/usr/share"]
    ordered = [
        user_dir,
        *data_dirs,
        f"{user_dir}/flatpak/exports/share",
        *_FLATPAK_DIRS,
        _SNAP_DIR,
    ]
    unique = dict.fromkeys(ordered)
    return tuple((f"{d}/applications", _source_of(d, user_dir)) for d in unique)


def _read_small(path: str) -> bytes | None:
    """A regular file of at most ``MAX_FILE_BYTES``, else None. Links are followed: Flatpak
    exports are links into the installed application."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        data = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= MAX_FILE_BYTES else None


def parse_desktop_entry(data: bytes) -> dict[str, str] | None:
    """The wanted keys of the ``[Desktop Entry]`` group; None if this is not a desktop file.

    Only that group is read, only the unlocalized keys, and the first value of a key wins.
    """
    if b"\0" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    values: dict[str, str] | None = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if values is not None:
                break  # the next group: actions and vendor extensions are not read
            if line != _GROUP:
                return None
            values = {}
            continue
        if values is None:
            return None  # a key before any group
        key, separator, value = line.partition("=")
        key = key.rstrip()
        if separator and key in _KEYS and key not in values:
            values[key] = value.strip()
    return values


def _first_words(value: str, limit: int = 8) -> list[str] | None:
    """The first few arguments of an Exec line, split the way the spec says (spaces, double
    quotes). None if a quote is left open."""
    words: list[str] = []
    position = 0
    while position < len(value) and len(words) < limit:
        if value[position] == " ":
            position += 1
            continue
        if value[position] == '"':
            end = value.find('"', position + 1)
            if end < 0:
                return None
            words.append(value[position + 1 : end])
            position = end + 1
        else:
            end = value.find(" ", position)
            end = len(value) if end < 0 else end
            words.append(value[position:end])
            position = end
    return words


def exec_name(value: str) -> str:
    """The base name of the program an ``Exec`` line would start, or "".

    A leading ``env NAME=value`` is looked through (snap and some vendors start that way).
    Anything that is not a plain file name gives "": so does every line with a shell
    metacharacter in its program word. The line itself is never executed or kept.
    """
    words = _first_words(value[:MAX_EXEC_CHARS].replace("\t", " "))
    if not words:
        return ""
    if posixpath.basename(words[0]) == "env":
        words = words[1:]
        while words and _ENV_ASSIGNMENT.match(words[0]):
            words = words[1:]
    if not words:
        return ""
    name = posixpath.basename(words[0])
    return name if _FILE_NAME.fullmatch(name) else ""


def _desktop_files(directory: str, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, str]]:
    """(desktop id, path) for the ``.desktop`` files below one application directory."""
    try:
        with os.scandir(directory) as listing:
            entries = sorted(
                (entry for _, entry in zip(range(MAX_ENTRIES_PER_DIR), listing, strict=False)),
                key=lambda entry: entry.name,
            )
    except OSError:
        return
    for entry in entries:
        try:
            if entry.name.endswith(_SUFFIX) and entry.is_file():
                yield prefix + entry.name[: -len(_SUFFIX)], entry.path
            elif depth < MAX_DEPTH and entry.is_dir(follow_symlinks=False):
                yield from _desktop_files(entry.path, f"{prefix}{entry.name}-", depth + 1)
        except OSError:
            continue


def _one_line(text: str, limit: int) -> str:
    """Cleaned text on a single line: a name or an id has no business holding a line break."""
    return " ".join(clean(text[: limit * 4], limit).split())


def _app(
    desktop_id: str, entry: dict[str, str], source: Source, packs: PackIndex | None
) -> InstalledApp:
    program = exec_name(entry.get("Exec", ""))
    icon = entry.get("Icon", "")
    categories = tuple(
        part for part in entry.get("Categories", "").split(";")[:64] if _CATEGORY.fullmatch(part)
    )[:MAX_CATEGORIES]
    pack = packs.match(desktop_id=desktop_id, binary=program) if packs is not None else None
    return InstalledApp(
        desktop_id=desktop_id,
        name=_one_line(entry.get("Name", ""), 64),
        exec_name=program,
        icon=icon if _ICON_NAME.fullmatch(icon) else "",
        categories=categories,
        no_display=entry.get("NoDisplay") == "true",
        source=source,
        pack_id=pack.id if pack is not None else None,
    )


def scan_installed(
    *,
    home: Path,
    environ: Mapping[str, str],
    root: Path = Path("/"),
    packs: PackIndex | None = None,
) -> tuple[InstalledApp, ...]:
    """The installed applications, one per desktop id, sorted by id. Blocking; never raises.

    The first directory that has an id wins (XDG precedence), so a file in your own
    directory replaces the system's, and one with ``Hidden=true`` removes the application.
    A file that cannot be read or parsed is ignored and hides nothing. ``root`` is where the
    absolute directories are looked up (a fixture tree in tests).
    """
    apps: dict[str, InstalledApp | None] = {}
    skipped = 0
    opened = 0
    try:
        for logical, source in application_dirs(home, environ):
            for desktop_id, path in _desktop_files(str(root / logical.lstrip("/"))):
                if opened >= MAX_FILES:
                    break  # this directory is done; the others end the same way
                desktop_id = _one_line(desktop_id, 128)
                if not desktop_id or desktop_id in apps or len(apps) >= MAX_APPS:
                    continue
                opened += 1
                data = _read_small(path)
                entry = parse_desktop_entry(data) if data is not None else None
                if entry is None:
                    skipped += 1
                    continue
                listed = entry.get("Type") == "Application" and entry.get("Hidden") != "true"
                apps[desktop_id] = _app(desktop_id, entry, source, packs) if listed else None
    except Exception:  # untrusted files: a parser bug must not escape
        log.warning("inventory_failed")
    found = tuple(app for _, app in sorted(apps.items()) if app is not None)
    log.event("inventory_scanned", apps=len(found), skipped=skipped)
    return found


class AppInventory:
    """The inventory, built on first use and kept until ``invalidate``. Thread-safe."""

    def __init__(self, scan: Callable[[], tuple[InstalledApp, ...]]) -> None:
        self._scan = scan
        self._lock = threading.Lock()
        self._apps: tuple[InstalledApp, ...] | None = None

    def apps(self) -> tuple[InstalledApp, ...]:
        """Every installed application. The first call scans (blocking file reads)."""
        with self._lock:
            if self._apps is None:
                self._apps = self._scan()
            return self._apps

    def get(self, desktop_id: str) -> InstalledApp | None:
        wanted = desktop_id.lower()
        return next((app for app in self.apps() if app.desktop_id.lower() == wanted), None)

    def invalidate(self) -> None:
        """Forget the scan; the next ``apps()`` reads the directories again."""
        with self._lock:
            self._apps = None
