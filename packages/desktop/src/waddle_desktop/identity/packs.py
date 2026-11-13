"""Identity packs: what Waddle knows about one application (ARCHITECTURE §4.6, steps 3-6).

A pack is a small first-party TOML file in ``knowledge/apps/``. It says how to recognise the
application (window classes, desktop ids, binary names), where its version can be read
without touching it (a window-title pattern, a ``--version`` flag for the sandbox), and where
its About dialog is, for the "ask you" step.

Packs are first-party, but they are still loaded defensively: a strict schema, size caps, and
a static check that refuses regular expressions which could backtrack badly on a hostile
window title. A bad file is skipped and reported; loading never raises.
"""

from __future__ import annotations

import math
import os
import posixpath
import re
import stat as stat_module
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
    field_validator,
)

from waddle_core.link import WindowContext
from waddle_desktop import log
from waddle_desktop.identity.cache import AppIdentity
from waddle_desktop.resources import data_base

MAX_PACK_BYTES: Final = 16 * 1024
MAX_PACKS: Final = 200
MAX_PATTERN_CHARS: Final = 200
MAX_CAPTION_CHARS: Final = 200
MAX_PROBE_CHARS: Final = 4096  # of a probed program's output
# Budget for (ways a pattern can match) x (characters of text it is searched in). Measured:
# a hand-made worst case at this budget takes under a second; the shipped packs use a
# fraction of it and run in microseconds.
_MAX_WORK: Final = 1 << 24

_PACK_ID: Final = r"^[a-z][a-z0-9-]{0,31}$"
Key = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
]
Label = Annotated[str, StringConstraints(min_length=1, max_length=48, pattern=r"^[^\x00-\x1f]+$")]
Pattern = Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATTERN_CHARS)]
# The same set as sandbox.readonly_exec.VERSION_FLAGS (a test keeps the two equal).
VersionFlag = Literal["--version", "-version", "-V"]


class UnsafePattern(ValueError):
    """A regular expression that the static check refuses."""


def _children(op: object, value: object) -> Iterator[tuple[object, bool]]:
    """(sub-pattern, is it the body of a repeat?) for one parsed node."""
    name = str(op)
    if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
        assert isinstance(value, tuple)
        yield value[2], True
    elif name == "SUBPATTERN":
        assert isinstance(value, tuple)
        yield value[3], False
    elif name == "BRANCH":
        assert isinstance(value, tuple)
        for alternative in value[1]:
            yield alternative, False
    elif name in ("ASSERT", "ASSERT_NOT"):
        assert isinstance(value, tuple)
        yield value[1], False
    elif name == "ATOMIC_GROUP":
        yield value, False


def _ways(pattern: object, in_loop: bool, text_chars: int) -> int:
    """An upper bound on the ways the matcher can walk ``pattern`` from one start position.

    Every repeat multiplies by the number of counts it may settle on, every alternation by
    its alternatives. That is the most backtracking can explore, because a repeated group
    holds no further choice (which is checked here too).
    """
    ways = 1
    for op, value in pattern:  # type: ignore[attr-defined]
        name = str(op)
        if name in ("GROUPREF", "GROUPREF_EXISTS"):
            raise UnsafePattern("backreferences are not allowed")
        repeat = name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")
        if in_loop and (repeat or name == "BRANCH"):
            raise UnsafePattern("a repeated group may not contain a repeat or an alternative")
        loops = False
        if repeat:
            least, most = int(value[0]), min(int(value[1]), text_chars)
            loops = most > 1
            ways *= max(1, most - least + 1)
        inner = [
            _ways(child, in_loop or (is_body and loops), text_chars)
            for child, is_body in _children(op, value)
        ]
        ways *= sum(inner) if name == "BRANCH" else math.prod(inner)
        if ways * text_chars > _MAX_WORK:
            raise UnsafePattern("too many ways to match: fewer or tighter repeats, please")
    return ways


def check_pattern(
    pattern: str, *, version_group: bool, text_chars: int = MAX_CAPTION_CHARS
) -> re.Pattern[str]:
    """Compile ``pattern`` if it is short and cannot backtrack badly; else ``UnsafePattern``.

    The check is deliberately blunt. Inside anything that repeats more than once there may be
    no further repeat (not even ``?``) and no alternative, which rules out the ``(a+)+`` and
    ``(a|aa)+`` families; there are no backreferences; and the ways to match (see ``_ways``)
    times ``text_chars``, the longest text the pattern will be searched in, must fit a fixed
    budget. That also stops the un-nested blow-ups, such as thirty ``a?`` in a row. With
    ``version_group`` the pattern must have exactly one group, named ``version``.
    """
    if len(pattern) > MAX_PATTERN_CHARS:
        raise UnsafePattern("pattern too long")
    try:
        # The standard library's own parser: the only way to see the structure the matcher
        # will run. It is a private module, so any surprise from it refuses the pattern.
        parsed = re._parser.parse(pattern)  # type: ignore[attr-defined]
        _ways(parsed, False, text_chars)
        compiled = re.compile(pattern)
    except UnsafePattern:
        raise
    except RecursionError as error:
        raise UnsafePattern("pattern nested too deeply") from error
    except Exception as error:
        raise UnsafePattern("not a valid pattern") from error
    if version_group and (compiled.groups != 1 or list(compiled.groupindex) != ["version"]):
        raise UnsafePattern('pattern needs exactly one group, named "version"')
    return compiled


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _lowered(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.lower() for value in values))


class Match(_Model):
    """How the application shows up. All comparisons are exact and ignore case."""

    window_classes: tuple[Key, ...] = Field((), max_length=32)
    desktop_ids: tuple[Key, ...] = Field((), max_length=32)
    binaries: tuple[Key, ...] = Field((), max_length=32)

    _lower = field_validator("window_classes", "desktop_ids", "binaries")(_lowered)


class CaptionVersion(_Model):
    """A pattern over the window title whose ``version`` group is the version."""

    pattern: Pattern

    @field_validator("pattern")
    @classmethod
    def _safe(cls, value: str) -> str:
        check_pattern(value, version_group=True)
        return value

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


class VersionProbe(_Model):
    """A flag that prints the version and exits, and a pattern over what it prints."""

    flag: VersionFlag
    pattern: Pattern

    @field_validator("pattern")
    @classmethod
    def _safe(cls, value: str) -> str:
        check_pattern(value, version_group=True, text_chars=MAX_PROBE_CHARS)
        return value

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.MULTILINE)


class WebApp(_Model):
    """An application that runs in a browser tab, recognised by the browser's window title."""

    title_patterns: tuple[Pattern, ...] = Field(min_length=1, max_length=8)

    @field_validator("title_patterns")
    @classmethod
    def _safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            check_pattern(pattern, version_group=False)
        return value

    def matches(self, caption: str) -> bool:
        text = caption[:MAX_CAPTION_CHARS]
        return any(re.search(pattern, text) for pattern in self.title_patterns)


class Pack(_Model):
    id: str = Field(pattern=_PACK_ID)
    name: Label
    match: Match = Match()
    browser: StrictBool = False  # its windows may show a web app: look at the title for one
    caption_version: CaptionVersion | None = None
    version_probe: VersionProbe | None = None
    web_app: WebApp | None = None
    about_path: tuple[Label, ...] = Field((), max_length=6)


@dataclass(frozen=True)
class PackError:
    """One pack file that was skipped. ``file`` is the file name only; for ``waddle doctor``."""

    file: str
    reason: str


def _reason(error: Exception) -> str:
    if isinstance(error, ValidationError):
        first = error.errors(include_url=False, include_input=False)[0]
        where = ".".join(str(part) for part in first["loc"])
        return f"{where}: {first['msg']}"[:200]
    if isinstance(error, tomllib.TOMLDecodeError):
        return "not valid TOML"
    if isinstance(error, UnicodeDecodeError):
        return "not UTF-8 text"
    if isinstance(error, OSError):
        return "cannot be read"
    return str(error)[:200] or "invalid"


def _load_one(path: Path) -> Pack:
    # Non-blocking, and regular files only: a pipe with this name must not stall start-up.
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY)
    with os.fdopen(fd, "rb") as handle:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        data = handle.read(MAX_PACK_BYTES + 1)
    if len(data) > MAX_PACK_BYTES:
        raise ValueError("file too large")
    pack = Pack.model_validate(tomllib.loads(data.decode("utf-8")))
    if path.name != f"{pack.id}.toml":
        raise ValueError("file name does not match the pack id")
    return pack


class PackIndex:
    """The loaded packs, with exact case-insensitive lookup. Immutable after construction."""

    def __init__(self, packs: tuple[Pack, ...] = (), errors: tuple[PackError, ...] = ()) -> None:
        kept: list[Pack] = []
        problems = list(errors)
        self._by_id: dict[str, Pack] = {}
        self._by_binary: dict[str, Pack] = {}
        seen: set[str] = set()
        for pack in packs:
            ids = {*pack.match.window_classes, *pack.match.desktop_ids}
            binaries = set(pack.match.binaries)
            # A name may belong to one pack only: otherwise which pack wins would depend on
            # load order. The later pack is dropped whole.
            if pack.id in seen or ids & self._by_id.keys() or binaries & self._by_binary.keys():
                problems.append(PackError(f"{pack.id}.toml", "matches the same name as another"))
                continue
            seen.add(pack.id)
            kept.append(pack)
            self._by_id.update(dict.fromkeys(ids, pack))
            self._by_binary.update(dict.fromkeys(binaries, pack))
        self.packs: Final = tuple(kept)
        self.errors: Final = tuple(problems)

    def get(self, pack_id: str) -> Pack | None:
        return next((pack for pack in self.packs if pack.id == pack_id), None)

    def match(
        self, *, window_class: str = "", desktop_id: str = "", binary: str = ""
    ) -> Pack | None:
        """The pack that names this window class or desktop id, else this binary name."""
        for key in (desktop_id, window_class):
            pack = self._by_id.get(key.lower()) if key else None
            if pack is not None:
                return pack
        return self._by_binary.get(binary.lower()) if binary else None

    def web_app(self, caption: str) -> Pack | None:
        """The web-app pack whose title pattern matches a browser's window title."""
        for pack in self.packs:
            if pack.web_app is not None and pack.web_app.matches(caption):
                return pack
        return None

    def find(self, target: AppIdentity | WindowContext, *, caption: str = "") -> Pack | None:
        """The pack for an identified app or a focused window, or None.

        ``caption`` is the window title (taken from the window itself when one is given). It
        is only used when the match is a browser, to see whether the tab is a known web app;
        the web app's pack is then returned in place of the browser's.
        """
        if isinstance(target, WindowContext):
            pack = self.match(window_class=target.resource_class)
            caption = caption or target.title
        else:
            pack = self.match(
                window_class=target.app_id,
                desktop_id=target.app_id,
                binary=posixpath.basename(target.exe),
            ) or self.match(binary=target.name)
        if pack is not None and pack.browser and caption:
            return self.web_app(caption) or pack
        return pack


def default_packs_dir() -> Path:
    """``knowledge/apps`` next to Waddle's other read-only data."""
    return data_base() / "knowledge" / "apps"


def load_packs(directory: Path) -> PackIndex:
    """Load every ``*.toml`` in ``directory``. Never raises: bad files are skipped and listed
    in ``PackIndex.errors``; a missing directory gives an empty index with one error."""
    packs: list[Pack] = []
    errors: list[PackError] = []
    try:
        files = sorted(p for p in directory.iterdir() if p.suffix == ".toml")
    except OSError:
        files = []
        errors.append(PackError("", "pack directory cannot be read"))
    if len(files) > MAX_PACKS:
        errors.append(PackError("", "too many pack files"))
        files = files[:MAX_PACKS]
    for path in files:
        try:
            packs.append(_load_one(path))
        except Exception as error:  # a bad pack must never stop Waddle from starting
            errors.append(PackError(path.name[:64], _reason(error)))
    index = PackIndex(tuple(packs), tuple(errors))
    log.event("packs_loaded", packs=len(index.packs), errors=len(index.errors))
    return index
