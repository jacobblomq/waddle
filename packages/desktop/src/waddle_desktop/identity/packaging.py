"""How an application was installed and which version the package metadata names
(ARCHITECTURE §4.6, step 2).

Only metadata *files* are read, through a tiny read-only ``FsReader``; no command is run.
Every read is capped, every string that came from outside is validated before it is used
to build another path, and ``detect_packaging`` returns UNKNOWN rather than raising.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat as stat_module
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice
from pathlib import Path
from typing import Final, Protocol

from waddle_desktop.identity.resolve import ResolvedBinary
from waddle_desktop.identity.snapshot import ProcessSnapshot
from waddle_desktop.identity.version import parse_version, version_text

MAX_PATH: Final = 4096
MAX_VERSION_CHARS: Final = 64
FLATPAK_METADATA_CAP: Final = 16 * 1024
XML_CAP: Final = 256 * 1024
SNAP_YAML_CAP: Final = 64 * 1024
VERSION_FILE_CAP: Final = 4096
PRODUCT_INFO_CAP: Final = 256 * 1024
DPKG_LIST_CAP: Final = 8 * 1024 * 1024
DPKG_STATUS_CAP: Final = 64 * 1024 * 1024
DPKG_MAX_LISTS: Final = 6000
LISTDIR_CAP: Final = 100_000
_LINE_CAP: Final = 8192
_INI_MAX_LINES: Final = 400


class Packaging(StrEnum):
    FLATPAK = "flatpak"
    SNAP = "snap"
    APPIMAGE = "appimage"
    DEB = "deb"
    OPT = "opt"  # /opt/<name>/ or an unpacked tarball in the home directory
    SYSTEM = "system"  # under /usr or /bin but not owned by a package we can read
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PackageInfo:
    kind: Packaging
    package_id: str
    version: str | None  # raw, as the metadata spells it
    channel: str | None  # Flatpak branch; None elsewhere
    # "flatpak-metadata" | "flatpak-info" | "snap-env" | "snap-yaml" | "appimage-name" |
    # "dpkg" | "version-file" | "dir-name" | "unknown"
    source: str


UNKNOWN_PACKAGE: Final = PackageInfo(Packaging.UNKNOWN, "", None, None, "unknown")


class FsReader(Protocol):
    """The whole filesystem surface of package detection: four reads and a listing."""

    def read_text(self, path: str, cap: int) -> str | None: ...
    def iter_lines(self, path: str, cap: int) -> Iterator[str]: ...
    def exists(self, path: str) -> bool: ...
    def listdir(self, path: str) -> list[str]: ...
    def stat(self, path: str) -> tuple[int, int] | None: ...


class RealFs:
    """``FsReader`` over the real filesystem, optionally re-rooted (fixtures).

    Paths are absolute and normalised; anything else is treated as missing. Only regular
    files are read, opened non-blocking, so a FIFO or device named ``VERSION`` in someone's
    application directory cannot hang or feed the reader.
    """

    def __init__(self, root: Path = Path("/")) -> None:
        self._root = root

    def _real(self, path: str) -> Path | None:
        if not isinstance(path, str) or not path.startswith("/") or len(path) > MAX_PATH:
            return None
        if "\0" in path or ".." in path.split("/"):
            return None
        return self._root / path.lstrip("/")

    def _open(self, path: str) -> int | None:
        real = self._real(path)
        if real is None:
            return None
        try:
            # Checked before the open as well: opening a FIFO or a device is already an
            # event for whoever is on the other side.
            if not real.is_file():  # follows links, like the open below
                return None
            fd = os.open(real, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY)
        except (OSError, ValueError):
            return None
        try:
            if stat_module.S_ISREG(os.fstat(fd).st_mode):
                return fd
        except OSError:
            pass
        os.close(fd)
        return None

    def read_text(self, path: str, cap: int) -> str | None:
        fd = self._open(path)
        if fd is None:
            return None
        try:
            chunks: list[bytes] = []
            remaining = cap
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError:
            return None
        finally:
            os.close(fd)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def iter_lines(self, path: str, cap: int) -> Iterator[str]:
        """Lines of a file, streamed, at most ``cap`` bytes in total; over-long lines are
        skipped whole so their tail cannot pose as a line of its own."""
        fd = self._open(path)
        if fd is None:
            return
        try:
            with os.fdopen(fd, "rb") as handle:
                remaining = cap
                at_line_start = True
                while remaining > 0:
                    piece = handle.readline(min(_LINE_CAP, remaining))
                    if not piece:
                        break
                    remaining -= len(piece)
                    complete = piece.endswith(b"\n")
                    if at_line_start and complete:
                        yield piece[:-1].decode("utf-8", errors="replace")
                    at_line_start = complete
        except OSError:
            return

    def exists(self, path: str) -> bool:
        real = self._real(path)
        try:
            return real is not None and real.exists()
        except (OSError, ValueError):
            return False

    def listdir(self, path: str) -> list[str]:
        real = self._real(path)
        if real is None:
            return []
        try:
            with os.scandir(real) as entries:
                return sorted(entry.name for entry in islice(entries, LISTDIR_CAP))
        except (OSError, ValueError):
            return []

    def stat(self, path: str) -> tuple[int, int] | None:
        real = self._real(path)
        if real is None:
            return None
        try:
            status = real.stat()
        except (OSError, ValueError):
            return None
        return status.st_size, status.st_mtime_ns


# --------------------------------------------------------------------------- helpers


def _clean_version(text: object) -> str | None:
    """A version string fit to store: short, single-line, printable, with a number in it."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > MAX_VERSION_CHARS or not text.isprintable():
        return None
    return text if parse_version(text) is not None else None


def _safe_path(path: object) -> str | None:
    """A normalised absolute path with no control characters, or None."""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > MAX_PATH:
        return None
    if any(ord(c) < 32 for c in path) or ".." in path.split("/"):
        return None
    return posixpath.normpath(path)


def _parse_ini(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in islice(text.splitlines(), _INI_MAX_LINES):
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
        elif current is not None and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            current.setdefault(key.strip(), value.strip())
    return sections


# --------------------------------------------------------------------------- Flatpak

_FLATPAK_ID: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+")
_FLATPAK_REF_PART: Final = re.compile(r"[A-Za-z0-9._-]{1,128}")
_RELEASE: Final = re.compile(
    r"""<release\s[^<>]{0,512}?\bversion\s*=\s*(["'])([^"'<>]{1,64})\1""", re.IGNORECASE
)


def flatpak_app_id(chain: Sequence[ProcessSnapshot]) -> str | None:
    """The Flatpak application id of the nearest sandboxed process in ``chain``, validated."""
    for snapshot in chain:
        candidates = [snapshot.env.get("FLATPAK_ID", "")]
        if snapshot.root_flatpak_info is not None:
            info = _parse_ini(snapshot.root_flatpak_info)
            candidates.insert(0, info.get("Application", {}).get("name", ""))
        for candidate in candidates:
            if len(candidate) <= 255 and _FLATPAK_ID.fullmatch(candidate):
                return candidate
    return None


def flatpak_commit(chain: Sequence[ProcessSnapshot]) -> str | None:
    """The deployed commit the running Flatpak app was started from (hex), if stated."""
    for snapshot in chain:
        if snapshot.root_flatpak_info is not None:
            instance = _parse_ini(snapshot.root_flatpak_info).get("Instance", {})
            commit = instance.get("app-commit", "")
            return commit if re.fullmatch(r"[0-9a-f]{8,64}", commit) else None
    return None


def _is_flatpak(chain: Sequence[ProcessSnapshot]) -> bool:
    return any(
        s.root_flatpak_info is not None
        or "FLATPAK_ID" in s.env
        or s.env.get("container") == "flatpak"
        for s in chain
    )


def _flatpak(chain: Sequence[ProcessSnapshot], fs: FsReader, home: str | None) -> PackageInfo:
    app_id = flatpak_app_id(chain)
    if app_id is None:
        return PackageInfo(Packaging.FLATPAK, "", None, None, "unknown")
    instance: dict[str, str] = {}
    for snapshot in chain:
        if snapshot.root_flatpak_info is not None:
            instance = _parse_ini(snapshot.root_flatpak_info).get("Instance", {})
            break
    branch = instance.get("branch", "")
    channel = branch if _FLATPAK_REF_PART.fullmatch(branch) else None

    installations = ["/var/lib/flatpak"]
    if home:
        installations.append(f"{home}/.local/share/flatpak")
    deploys: list[str] = []
    # app-path names the exact deployment that is running ("<inst>/app/<id>/<arch>/<branch>/
    # <commit>/files"). It is only believed when it lies inside a known installation.
    app_path = _safe_path(instance.get("app-path"))
    if (
        app_path
        and app_path.endswith("/files")
        and any(app_path.startswith(f"{inst}/app/{app_id}/") for inst in installations)
    ):
        deploys.append(posixpath.dirname(app_path))
    deploys += [f"{inst}/app/{app_id}/current/active" for inst in installations]

    for deploy in deploys:
        metadata = fs.read_text(f"{deploy}/metadata", FLATPAK_METADATA_CAP)
        if metadata is None:
            continue
        declared = _parse_ini(metadata).get("Application", {}).get("name")
        if declared is not None and declared != app_id:
            continue
        # The version is not in .flatpak-info or metadata: the newest <release> of the
        # app's own AppStream file is the best side-effect-free source.
        for directory in ("export/share/metainfo", "export/share/appdata", "files/share/metainfo"):
            for suffix in ("metainfo.xml", "appdata.xml"):
                xml = fs.read_text(f"{deploy}/{directory}/{app_id}.{suffix}", XML_CAP)
                release = _RELEASE.search(xml) if xml else None
                version = _clean_version(release.group(2)) if release else None
                if version:
                    return PackageInfo(
                        Packaging.FLATPAK, app_id, version, channel, "flatpak-metadata"
                    )
        break
    return PackageInfo(Packaging.FLATPAK, app_id, None, channel, "flatpak-info")


# --------------------------------------------------------------------------- Snap

_SNAP_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,39}(?:_[a-z0-9]{1,10})?")
_SNAP_CGROUP: Final = re.compile(r"/snap\.([a-z0-9][a-z0-9-]{0,39}(?:_[a-z0-9]{1,10})?)\.[^/\n]+")
_SNAP_EXE: Final = re.compile(r"^/snap/([a-z0-9][a-z0-9_-]{0,50})/(x?\d{1,10})/")
_SNAP_YAML_VERSION: Final = re.compile(r"^version:[ \t]*(.{1,128}?)[ \t]*$", re.MULTILINE)


def snap_name(chain: Sequence[ProcessSnapshot]) -> str | None:
    """The snap (instance) name of the nearest snap-confined process in ``chain``."""
    for snapshot in chain:
        candidates = [
            snapshot.env.get("SNAP_INSTANCE_NAME", ""),
            snapshot.env.get("SNAP_NAME", ""),
        ]
        exe = _SNAP_EXE.match(snapshot.exe or "")
        if exe:
            candidates.append(exe.group(1))
        cgroup = _SNAP_CGROUP.search(snapshot.cgroup)
        if cgroup:
            candidates.append(cgroup.group(1))
        for candidate in candidates:
            if _SNAP_NAME.fullmatch(candidate):
                return candidate
    return None


def snap_revision(chain: Sequence[ProcessSnapshot]) -> str | None:
    for snapshot in chain:
        revision = snapshot.env.get("SNAP_REVISION", "")
        if re.fullmatch(r"x?\d{1,10}", revision):
            return revision
        exe = _SNAP_EXE.match(snapshot.exe or "")
        if exe:
            return exe.group(2)
    return None


def _snap(chain: Sequence[ProcessSnapshot], fs: FsReader, name: str) -> PackageInfo:
    for snapshot in chain:
        version = _clean_version(snapshot.env.get("SNAP_VERSION"))
        if version and "SNAP_NAME" in snapshot.env:
            return PackageInfo(Packaging.SNAP, name, version, None, "snap-env")
    revision = snap_revision(chain) or "current"
    yaml = fs.read_text(f"/snap/{name}/{revision}/meta/snap.yaml", SNAP_YAML_CAP)
    found = _SNAP_YAML_VERSION.search(yaml) if yaml else None
    version = _clean_version(found.group(1).strip("'\"")) if found else None
    return PackageInfo(Packaging.SNAP, name, version, None, "snap-yaml" if version else "unknown")


# --------------------------------------------------------------------------- AppImage


def _appimage(path: str) -> PackageInfo:
    name = posixpath.basename(path)[:255]
    stem = name[: -len(".appimage")] if name.lower().endswith(".appimage") else name
    # The file name is the only version source that needs neither mounting nor running it.
    return PackageInfo(Packaging.APPIMAGE, name, version_text(stem), None, "appimage-name")


# --------------------------------------------------------------------------- deb

_DPKG_INFO: Final = "/var/lib/dpkg/info"
_DPKG_STATUS: Final = "/var/lib/dpkg/status"
_USRMERGE: Final = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")
_DEB_PREFIXES: Final = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/opt/")
_DEB_NAME: Final = re.compile(r"[a-z0-9][a-z0-9+.-]{1,127}")


def _usrmerge_aliases(path: str) -> tuple[str, ...]:
    for directory in _USRMERGE:
        if path.startswith(f"/usr/{directory}/"):
            return path, path[len("/usr") :]
        if path.startswith(f"/{directory}/"):
            return path, "/usr" + path
    return (path,)


def _has_line(text: str, line: str) -> bool:
    # dpkg ends every line with a newline, so a line cut off by the read cap never matches.
    start = text.find(line)
    while start >= 0:
        end = start + len(line)
        if (start == 0 or text[start - 1] == "\n") and text[end : end + 1] == "\n":
            return True
        start = text.find(line, start + 1)
    return False


def _dpkg_owner(fs: FsReader, paths: Sequence[str]) -> str | None:
    """The package whose file list contains one of ``paths`` as an exact line ("pkg[:arch]")."""
    lists = [name for name in fs.listdir(_DPKG_INFO) if name.endswith(".list")]
    wanted = posixpath.basename(paths[0]).lower()

    def likelihood(name: str) -> int:
        package = name[: -len(".list")].split(":")[0]
        if package == wanted:
            return 0
        return 1 if wanted and (wanted in package or package in wanted) else 2

    lists.sort(key=likelihood)  # stable: most packages ship the binary they are named after
    for name in lists[:DPKG_MAX_LISTS]:
        text = fs.read_text(f"{_DPKG_INFO}/{name}", DPKG_LIST_CAP)
        if not text:
            continue
        if any(_has_line(text, path) for path in paths):
            return name[: -len(".list")]
    return None


def _dpkg_version(fs: FsReader, package: str, architecture: str) -> str | None:
    name = status = version = arch = ""

    def matches() -> bool:
        installed = status.split()[-1:] == ["installed"]
        return name == package and installed and (not architecture or arch == architecture)

    for line in fs.iter_lines(_DPKG_STATUS, DPKG_STATUS_CAP):
        if not line.strip():
            if matches():
                return _clean_version(version)
            name = status = version = arch = ""
        elif line.startswith("Package:"):
            name = line[8:].strip()
        elif line.startswith("Status:"):
            status = line[7:].strip()
        elif line.startswith("Version:"):
            version = line[8:].strip()
        elif line.startswith("Architecture:"):
            arch = line[13:].strip()
    return _clean_version(version) if matches() else None


def _deb(fs: FsReader, path: str) -> PackageInfo | None:
    owner = _dpkg_owner(fs, _usrmerge_aliases(path))
    if owner is None:
        return None
    package, _, architecture = owner.partition(":")
    if not _DEB_NAME.fullmatch(package):
        return None
    version = _dpkg_version(fs, package, architecture)
    return PackageInfo(Packaging.DEB, package, version, None, "dpkg")


# --------------------------------------------------------------------------- /opt, tarballs

_VERSION_FILES: Final = ("VERSION", "version", "version.txt", ".version", "release")
_GENERIC_DIRS: Final = frozenset({"bin", "sbin", "lib", "lib64", "libexec", "usr", "share"})
# An Electron distribution ships a file called "version" holding *Electron's* version.
_ELECTRON_MARKERS: Final = ("resources/app.asar", "resources/app", "chrome_100_percent.pak")


def _version_from_files(fs: FsReader, directory: str) -> str | None:
    electron: bool | None = None
    for name in _VERSION_FILES:
        text = fs.read_text(f"{directory}/{name}", VERSION_FILE_CAP)
        if not text:
            continue
        if name == "version":
            if electron is None:
                electron = any(fs.exists(f"{directory}/{m}") for m in _ELECTRON_MARKERS)
            if electron:
                continue
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        # A bare version is kept as written; from a sentence only the version is taken.
        version = _clean_version(first) if " " not in first else None
        version = version or version_text(first)
        if version:
            return version
    return None


def _product_info(fs: FsReader, directory: str) -> tuple[str | None, str | None]:
    """(version, data directory name) from a JetBrains-style product-info.json."""
    text = fs.read_text(f"{directory}/product-info.json", PRODUCT_INFO_CAP)
    if not text:
        return None, None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    data_dir = data.get("dataDirectoryName")
    return _clean_version(data.get("version")), data_dir if isinstance(data_dir, str) else None


def _opt(fs: FsReader, path: str, boundary: str, *, inclusive: bool) -> PackageInfo:
    """Version files and versioned directory names around ``path``, never above ``boundary``
    (inclusive for /opt/<name>, exclusive for the home directory)."""
    directories: list[str] = []
    directory = posixpath.dirname(path)
    for _ in range(3):  # the application directory and up to two parents
        inside = directory.startswith(boundary + "/") or (inclusive and directory == boundary)
        if not inside:
            break
        directories.append(directory)
        directory = posixpath.dirname(directory)
    named = [d for d in directories if posixpath.basename(d) not in _GENERIC_DIRS]
    package_id = posixpath.basename(named[0] if named else path)[:255]

    data_dirs: list[str] = []
    for directory in directories:
        version = _version_from_files(fs, directory)
        if version is None:
            version, data_dir = _product_info(fs, directory)
            if data_dir:
                data_dirs.append(data_dir)
        if version:
            return PackageInfo(Packaging.OPT, package_id, version, None, "version-file")
    for directory in directories:
        version = version_text(posixpath.basename(directory))
        if version:
            return PackageInfo(Packaging.OPT, package_id, version, None, "dir-name")
    for data_dir in data_dirs:
        version = version_text(data_dir[:256])
        if version:
            return PackageInfo(Packaging.OPT, package_id, version, None, "version-file")
    return PackageInfo(Packaging.OPT, package_id, None, None, "unknown")


# --------------------------------------------------------------------------- entry point


def _detect(
    binary: ResolvedBinary, chain: Sequence[ProcessSnapshot], fs: FsReader, home: str | None
) -> PackageInfo:
    if _is_flatpak(chain):
        return _flatpak(chain, fs, home)
    name = snap_name(chain)
    if name is not None:
        return _snap(chain, fs, name)
    if "appimage" in binary.wrapper_kinds or binary.exe.lower().endswith(".appimage"):
        return _appimage(binary.exe)

    path = _safe_path(binary.identity_path)
    if path is None:
        return UNKNOWN_PACKAGE
    in_home = bool(home) and path.startswith(f"{home}/")
    if not in_home and path.startswith(_DEB_PREFIXES) and not path.startswith("/usr/local/"):
        owned = _deb(fs, path)
        if owned is not None:
            return owned
    if in_home and home:
        return _opt(fs, path, home, inclusive=False)
    if path.startswith("/opt/"):
        parts = path.split("/")
        if len(parts) > 3:
            return _opt(fs, path, f"/opt/{parts[2]}", inclusive=True)
        return PackageInfo(Packaging.OPT, parts[2][:255], None, None, "unknown")
    if path.startswith(("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/")):
        return PackageInfo(Packaging.SYSTEM, posixpath.basename(path)[:255], None, None, "unknown")
    return UNKNOWN_PACKAGE


def detect_packaging(
    binary: ResolvedBinary,
    chain: Sequence[ProcessSnapshot],
    fs: FsReader,
    *,
    home: Path | str | None = None,
) -> PackageInfo:
    """Packaging kind, package id and metadata version for a resolved binary. Never raises."""
    try:
        home_text = _safe_path(str(home)) if home is not None else None
        if home_text == "/":
            home_text = None
        return _detect(binary, chain, fs, home_text)
    except Exception:  # metadata is untrusted: no parser bug may reach the caller
        return UNKNOWN_PACKAGE
