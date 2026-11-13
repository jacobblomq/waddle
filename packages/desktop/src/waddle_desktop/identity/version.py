"""Version strings from package metadata, file names and version files (ARCHITECTURE §4.6).

Everything here is a pure function over short strings that come from outside Waddle
(package databases, file names, files inside application directories). Input is capped and
nothing raises on garbage.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

MAX_TEXT: Final = 256
MAX_COMPONENT: Final = 100_000

# Lowest to highest. A pre-release sorts below the release it leads up to.
_PRE_RANK: Final = {"nightly": 0, "alpha": 1, "beta": 2, "rc": 3, "": 4}


@dataclass(frozen=True, order=True)
class Version:
    """A comparable ``major.minor[.patch]`` with an optional pre-release tag.

    Equality and ordering treat a missing patch as 0 (``4.5 == 4.5.0``) and rank
    ``nightly < alpha < beta < rc < release``; ``patch`` and ``pre`` keep what was written.
    """

    major: int
    minor: int
    patch: int | None = field(compare=False)
    pre: str = field(default="", compare=False)
    _order: tuple[int, int] = field(init=False, repr=False, compare=True, default=(0, 0))

    def __post_init__(self) -> None:
        numbers = (self.major, self.minor, 0 if self.patch is None else self.patch)
        if any(isinstance(n, bool) or not isinstance(n, int) for n in numbers):
            raise ValueError("version components must be integers")
        if any(n < 0 or n > MAX_COMPONENT for n in numbers):
            raise ValueError("version component out of range")
        if self.pre not in _PRE_RANK:
            raise ValueError("unknown pre-release tag")
        object.__setattr__(self, "_order", (numbers[2], _PRE_RANK[self.pre]))

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}"
        if self.patch is not None:
            text += f".{self.patch}"
        return f"{text}-{self.pre}" if self.pre else text


class Channel(StrEnum):
    STABLE = "stable"
    LTS = "lts"  # never inferred from a version number: only when the text says so
    BETA = "beta"
    ALPHA = "alpha"
    NIGHTLY = "nightly"
    RC = "rc"


# A dotted number that does not start in the middle of a word or of another number, so
# "python3.12", "x86_64" and hex build hashes do not count.
_DOTTED: Final = re.compile(r"(?<![0-9A-Za-z.])[vV]?(\d{1,7}(?:\.\d{1,7}){1,7})(?![0-9])")
# "Release Candidate" is how About boxes and splash screens spell out "rc".
_WORDS: Final = "alpha|beta|rc|release[ _-]?candidate|nightly|daily|snapshot|preview|pre|dev"
# The whole string is one integer, optionally with a pre-release word ("132", "v7 beta").
_BARE: Final = re.compile(rf"[vV]?(\d{{1,6}})(?:[ ._~+-]?(?:{_WORDS})\d*)?", re.IGNORECASE)
_EPOCH: Final = re.compile(r"^\d{1,4}:")
# Directly attached short forms: "21.0b3", "3.13.0a2", "1.2rc1". A bare trailing letter
# ("1.0.2b") is a patch letter, not a beta.
_ATTACHED: Final = re.compile(r"(?:(a|b)\d+|(rc)\d*)(?![A-Za-z])", re.IGNORECASE)
_PRE_WORD: Final = re.compile(rf"(?<![A-Za-z])({_WORDS})(?![A-Za-z])", re.IGNORECASE)
_LTS: Final = re.compile(r"(?<![A-Za-z])lts(?![A-Za-z])", re.IGNORECASE)
_PRE_NAMES: Final = {
    "a": "alpha",
    "alpha": "alpha",
    "b": "beta",
    "beta": "beta",
    "pre": "beta",
    "preview": "beta",
    "rc": "rc",
    "nightly": "nightly",
    "daily": "nightly",
    "snapshot": "nightly",
    "dev": "nightly",
}
_TAIL: Final = 48


def _pre_name(word: str) -> str:
    """The canonical tag for a matched pre-release word or short form."""
    word = word.lower()
    return "rc" if word.startswith("release") else _PRE_NAMES[word]


def _as_text(value: object) -> str:
    """Callers pass strings from D-Bus and files: anything else counts as empty."""
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _pre_of(tail: str) -> str:
    tail = tail[:_TAIL]
    attached = _ATTACHED.match(tail)
    if attached:
        return _pre_name(attached.group(1) or attached.group(2))
    word = _PRE_WORD.search(tail)
    return _pre_name(word.group(1)) if word else ""


def _scan(text: object) -> tuple[Version, str] | None:
    """The version in ``text`` and its canonical raw form (every component, plus the tag)."""
    if not isinstance(text, str):
        return None
    text = _EPOCH.sub("", text[:MAX_TEXT].strip())
    match = _DOTTED.search(text)
    if match:
        digits, tail = match.group(1), text[match.end() :]
    else:
        bare = _BARE.fullmatch(text)
        if not bare:
            return None
        digits, tail = bare.group(1), text[bare.end(1) :]
    parts = [int(p) for p in digits.split(".")]
    if any(p > MAX_COMPONENT for p in parts):
        return None
    pre = _pre_of(tail)
    version = Version(
        parts[0],
        parts[1] if len(parts) > 1 else 0,
        parts[2] if len(parts) > 2 else None,
        pre,
    )
    return version, (f"{digits}-{pre}" if pre else digits)


def parse_version(text: str) -> Version | None:
    """Best-effort version from distro, file-name or About-box noise; None when there is none."""
    found = _scan(text)
    return found[0] if found else None


def version_text(text: str) -> str | None:
    """The version part of ``text`` as a short clean string ("21.1.0.17", "5.3.0-alpha").

    ``parse_version(version_text(x)) == parse_version(x)`` whenever a version is found.
    """
    found = _scan(text)
    return found[1] if found else None


def docs_key(version: Version) -> str:
    return f"{version.major}.{version.minor}"


_CHANNEL_ORDER: Final = (
    ("nightly", Channel.NIGHTLY),
    ("alpha", Channel.ALPHA),
    ("beta", Channel.BETA),
    ("rc", Channel.RC),
)


def detect_channel(raw: str) -> Channel:
    """Release channel named by a raw version or product string; STABLE when it names none."""
    raw = _as_text(raw)
    seen = {_pre_name(m.group(1)) for m in _PRE_WORD.finditer(raw)}
    version = parse_version(raw)
    if version and version.pre:
        seen.add(version.pre)
    for name, channel in _CHANNEL_ORDER:
        if name in seen:
            return channel
    return Channel.LTS if _LTS.search(raw) else Channel.STABLE


# app-id fragment -> (edition, token that must appear as a whole word in a hint).
# Only editions that are *named* somewhere are reported; the absence of "Studio" does not
# make something the free edition.
_EDITIONS: Final[tuple[tuple[str, tuple[tuple[str, str], ...]], ...]] = (
    ("resolve", (("Studio", "studio"),)),
    ("pycharm", (("Community", "community"), ("Professional", "professional"))),
    ("idea", (("Community", "community"), ("Ultimate", "ultimate"))),
)
_MAX_HINTS: Final = 32


def detect_edition(app_id: str, hints: Iterable[str]) -> str | None:
    """Product edition named in installer, directory or desktop-file names; None if unnamed."""
    app = _as_text(app_id).lower()
    table = next((editions for fragment, editions in _EDITIONS if fragment in app), None)
    if table is None:
        return None
    texts: list[str] = []
    for count, hint in enumerate(hints):
        if count >= _MAX_HINTS:
            break
        if isinstance(hint, str):
            texts.append(hint[:MAX_TEXT].lower())
    for edition, token in table:
        pattern = re.compile(rf"(?<![a-z]){token}(?![a-z])")
        if any(pattern.search(text) for text in texts):
            return edition
    return None
