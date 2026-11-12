"""From a process chain to the binary that *is* the application (ARCHITECTURE §4.6, step 1).

Pure functions over ``ProcessSnapshot``: no I/O, and never further up than the chain given.
Command lines are chosen by the processes themselves, so every argument is treated as an
opaque, length-capped string.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from waddle_desktop.identity.snapshot import ProcessSnapshot

MAX_PATH: Final = 4096
MAX_NAME: Final = 64


@dataclass(frozen=True)
class ResolvedBinary:
    exe: str  # the application's executable ("" when unknown); the AppImage file for AppImages
    argv0: str
    wrapper_kinds: tuple[str, ...]  # what was seen through, outermost first
    script: str | None  # the program an interpreter or launcher is running

    @property
    def identity_path(self) -> str:
        """The path that names the application: the script if there is one, else the exe."""
        return self.script or self.exe


@dataclass(frozen=True)
class _Interpreter:
    kind: str
    name: re.Pattern[str]
    takes_value: frozenset[str] = frozenset()  # options followed by a separate value
    no_script: frozenset[str] = frozenset()  # options after which no script file follows
    script_after: str | None = None  # the script is the value of this option (java -jar)
    cluster_stop: str = ""  # a short-option cluster containing this letter means no script


_INTERPRETERS: Final = (
    _Interpreter(
        "python",
        re.compile(r"python(\d+(\.\d+)?)?"),
        takes_value=frozenset({"-W", "-X", "-Q", "--check-hash-based-pycs"}),
        no_script=frozenset({"-c", "-m", "-"}),
    ),
    _Interpreter(
        "shell",
        re.compile(r"(ba|da|z|k)?sh"),
        takes_value=frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}),
        no_script=frozenset({"-c", "-s"}),
        cluster_stop="c",
    ),
    _Interpreter(
        "perl",
        re.compile(r"perl(\d[\d.]*)?"),
        no_script=frozenset({"-e", "-E"}),
        cluster_stop="e",
    ),
    _Interpreter(
        "node",
        re.compile(r"node(js)?"),
        takes_value=frozenset({"-r", "--require", "--import", "--loader"}),
        no_script=frozenset({"-e", "--eval", "-p", "--print", "-i", "--interactive"}),
    ),
    _Interpreter(
        "java",
        re.compile(r"java"),
        takes_value=frozenset(
            {"-cp", "-classpath", "--class-path", "-p", "--module-path", "--add-modules"}
        ),
        script_after="-jar",
    ),
    _Interpreter("electron", re.compile(r"electron\d*")),
)

# AppImage runtimes mount the image under $TMPDIR/.mount_<name><random>/.
_APPIMAGE_MOUNT: Final = re.compile(r"^/(?:tmp|var/tmp|run/user/\d+)/\.mount_[^/]+/")
_BWRAP: Final = frozenset({"bwrap", "flatpak-bwrap"})
_SNAP_LAUNCHERS: Final = frozenset({"snap-confine", "snap-exec"})
_NIX_WRAPPED: Final = re.compile(r"^\.(.+)-wrapped$")


def _basename(path: str) -> str:
    return posixpath.basename(path.rstrip("/"))


def _is_helper(snapshot: ProcessSnapshot) -> bool:
    """Chromium/Electron children (renderer, gpu-process, zygote, utility...)."""
    return any(arg.startswith("--type=") for arg in snapshot.cmdline[1:])


def _looks_like_path(arg: str) -> bool:
    if not arg or len(arg) > MAX_PATH or any(ord(c) < 32 for c in arg):
        return False
    return "/" in arg or "." in arg


def _script(args: Sequence[str], spec: _Interpreter) -> str | None:
    """The script an interpreter was started with, from its arguments (argv[1:])."""
    skip = False
    take = False
    for position, arg in enumerate(args):
        if take:
            return arg if _looks_like_path(arg) else None
        if skip:
            skip = False
            continue
        if spec.script_after is not None:
            if arg == spec.script_after:
                take = True
            elif arg in spec.takes_value:
                skip = True
            elif not arg.startswith("-"):
                return None  # a main class, not a file
            continue
        if arg == "--":
            rest = args[position + 1 : position + 2]
            return rest[0] if rest and _looks_like_path(rest[0]) else None
        if arg in spec.no_script:
            return None
        if arg in spec.takes_value:
            skip = True
            continue
        if arg.startswith("-") or (spec.kind == "shell" and arg.startswith("+")):
            if spec.cluster_stop and not arg.startswith("--") and spec.cluster_stop in arg:
                return None
            continue
        return arg if _looks_like_path(arg) else None
    return None


def _appimage_file(chain: Sequence[ProcessSnapshot]) -> str | None:
    for snapshot in chain:
        path = snapshot.env.get("APPIMAGE", "")
        if path.startswith("/") and len(path) <= MAX_PATH and not any(ord(c) < 32 for c in path):
            return path
    return None


def _in_appimage(snapshot: ProcessSnapshot, exe: str) -> bool:
    if _APPIMAGE_MOUNT.match(exe):
        return True
    appdir = snapshot.env.get("APPDIR", "").rstrip("/")
    return bool(appdir) and appdir.startswith("/") and exe.startswith(appdir + "/")


def resolve_binary(chain: Sequence[ProcessSnapshot]) -> ResolvedBinary:
    """See through helpers, sandboxes, interpreters and wrapper names. Never raises."""
    if not chain:
        return ResolvedBinary("", "", (), None)
    kinds: list[str] = []
    index = 0
    # A focused Chromium/Electron helper is not the app: its nearest ancestor without
    # --type= is. If the chain ends first, the last one we were given has to do.
    while _is_helper(chain[index]) and index + 1 < len(chain):
        index += 1
    if index or _is_helper(chain[index]):
        kinds.append("helper")
    owner = chain[index]
    exe = (owner.exe or "")[:MAX_PATH]
    argv0 = owner.cmdline[0][:MAX_PATH] if owner.cmdline else ""
    name = _basename(exe or argv0)

    if owner.root_flatpak_info is not None or "FLATPAK_ID" in owner.env:
        kinds.append("flatpak")  # exe is a path inside the sandbox; identity is the app id
    if name in _BWRAP:
        kinds.append("bwrap")
    if name in _SNAP_LAUNCHERS:
        kinds.append("snap-confine")
    elif "SNAP_NAME" in owner.env or exe.startswith("/snap/"):
        kinds.append("snap")

    if _in_appimage(owner, exe):
        kinds.append("appimage")
        return ResolvedBinary(_appimage_file(chain[index:]) or exe, argv0, tuple(kinds), None)

    script: str | None = None
    for spec in _INTERPRETERS:
        if spec.name.fullmatch(name):
            script = _script(owner.cmdline[1:], spec)
            if script is not None:
                kinds.append(spec.kind)
            break
    if script is None and (
        _NIX_WRAPPED.match(name) or name.endswith(".wrapped") or name.endswith(".bin")
    ):
        kinds.append("wrapped")
    return ResolvedBinary(exe, argv0, tuple(kinds), script)


_SCRIPT_SUFFIXES: Final = (".py", ".pyw", ".sh", ".pl", ".js", ".mjs", ".cjs", ".jar", ".appimage")
_ELECTRON_CONTAINERS: Final = frozenset({"resources", "app", "app.asar"})
_VERSION_SUFFIX: Final = re.compile(r"[-_ ]v?\d+(?:(?:\.\d+)+(?:[-_. ].*)?)?$")
_ARCH_SUFFIX: Final = re.compile(
    r"[-_.](?:x86[_-]64|amd64|x64|aarch64|arm64|i[36]86|linux(?:64)?)$"
)
_UNSAFE: Final = re.compile(r"[^a-z0-9._+-]+")


def identity_name(binary: ResolvedBinary) -> str:
    """A short lower-case name for the application ("blender"), from its path only.

    Wrapper decorations (".x-wrapped", "x.bin"), script suffixes, version and architecture
    suffixes are stripped; the result contains only ``[a-z0-9._+-]`` and may be empty.
    """
    path = binary.identity_path.rstrip("/")
    if "electron" in binary.wrapper_kinds and binary.script:
        # "electron /usr/lib/element/resources/app.asar" is the app called "element".
        while _basename(path) in _ELECTRON_CONTAINERS and posixpath.dirname(path) not in ("", "/"):
            path = posixpath.dirname(path)
    name = _basename(path)[: MAX_NAME * 4]
    nix = _NIX_WRAPPED.match(name)
    if nix:
        name = nix.group(1)
    name = name.lower()
    for suffix in (".wrapped", ".bin", *_SCRIPT_SUFFIXES):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    for _ in range(4):
        stripped = _VERSION_SUFFIX.sub("", _ARCH_SUFFIX.sub("", name))
        if stripped == name or not stripped:
            break
        name = stripped
    return _UNSAFE.sub("-", name).strip("-.")[:MAX_NAME]
