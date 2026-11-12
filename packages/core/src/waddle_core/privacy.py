"""The privacy gate and the blocklist (ARCHITECTURE §1 I4, §4.4).

``gate`` is the single pure function every capture, accessibility read, rewind-buffer write,
adapter read and host tool call passes through. The capture helper enforces the same lock and
blocklist rules again on its side (§4.3); the matching semantics here and there are pinned by
the shared fixture ``packages/core/tests/blocklist_cases.json``.

Window captions and class names are untrusted text from other processes. Nothing in this
module returns or stores them: a ``BlockMatch`` carries only which list matched and at which
index, so it is safe to log.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from waddle_core.identity import APP_ID
from waddle_core.window import WindowInfo

# The default list is a small first-party file; anything bigger is not that file.
MAX_BLOCKLIST_BYTES = 1024 * 1024


class BlocklistError(ValueError):
    """The blocklist text is not in the expected format."""


# Characters that draw nothing and that NFKC keeps: soft hyphen, combining grapheme joiner,
# Arabic letter mark, Mongolian vowel separator, zero-width spaces and joiners, bidi marks,
# embeddings and isolates, word joiner and the other invisible operators, variation
# selectors, the byte-order mark, tag characters. The capture helper drops the same set
# (``is_invisible`` in capture-helper/src/blocklist.rs); the shared fixture pins both.
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f"
    "\ufe00-\ufe0f\ufeff\U000e0000-\U000e0fff]"
)


def normalize(text: str) -> str:
    """Fold ``text`` for matching: drop invisibles, NFKC, Unicode case folding, NFKC again.

    Compatibility forms (full-width letters, ligatures, decomposed accents), case variants
    of the same word, and a word with a soft hyphen or zero-width space inside it must not
    slip past a pattern, so both the pattern and the window text go through this before the
    substring test. Whitespace is not collapsed.
    """
    visible = _INVISIBLE.sub("", text)
    folded = unicodedata.normalize("NFKC", visible).casefold()
    return unicodedata.normalize("NFKC", folded)


def _clean(patterns: Iterable[object], where: str) -> tuple[str, ...]:
    """Validate and normalize patterns, dropping blank ones.

    An empty pattern is a substring of everything; it would blind Waddle everywhere, and is
    always a mistake, so it is dropped rather than honoured.
    """
    if isinstance(patterns, str):
        # A bare string would be read as one pattern per character.
        raise BlocklistError(f"{where} must be a sequence of strings, not one string")
    out: list[str] = []
    for index, pattern in enumerate(patterns):
        if not isinstance(pattern, str):
            raise BlocklistError(f"{where}[{index}] is not a string")
        folded = normalize(pattern)
        if folded.strip():
            out.append(folded)
    return tuple(out)


@dataclass(frozen=True)
class BlockMatch:
    """Which pattern blocked a window. Deliberately carries no window text."""

    field: Literal["class", "title"]
    pattern_index: int


@dataclass(frozen=True)
class Blocklist:
    """Case-insensitive substring patterns for windows Waddle must never look at.

    ``classes`` are tested against the window's resource class, resource name and desktop
    file name; ``titles`` against its caption. Patterns are stored normalized.
    """

    classes: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "classes", _clean(self.classes, "class"))
        object.__setattr__(self, "titles", _clean(self.titles, "title"))

    @classmethod
    def from_json(cls, text: str) -> Blocklist:
        """Parse the format of ``knowledge/privacy/default-blocklist.json``.

        Keys starting with ``_`` are comments. Any other unknown key is an error: a typo
        such as ``"titles"`` must not silently produce a list that blocks nothing.
        """
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise BlocklistError("blocklist is not valid JSON") from exc
        if not isinstance(data, dict):
            raise BlocklistError("blocklist must be a JSON object")
        unknown = sorted(k for k in data if not k.startswith("_") and k not in ("class", "title"))
        if unknown:
            raise BlocklistError(f"unknown blocklist keys: {', '.join(unknown)}")
        lists: dict[str, list[object]] = {}
        for key in ("class", "title"):
            value = data.get(key)
            if not isinstance(value, list):
                raise BlocklistError(f'blocklist key "{key}" must be a list of strings')
            lists[key] = value
        return cls(_clean(lists["class"], "class"), _clean(lists["title"], "title"))

    def merged(
        self, extra_classes: Iterable[str] = (), extra_titles: Iterable[str] = ()
    ) -> Blocklist:
        """This list plus the user's additions. Additions can only ever block more."""
        return Blocklist(
            self.classes + _clean(extra_classes, "extra_classes"),
            self.titles + _clean(extra_titles, "extra_titles"),
        )

    def match(self, window: WindowInfo) -> BlockMatch | None:
        """The first pattern that blocks ``window``, or ``None``. Class patterns go first."""
        identifiers = [
            normalize(window.resource_class),
            normalize(window.resource_name),
            normalize(window.desktop_file),
        ]
        for index, pattern in enumerate(self.classes):
            if any(pattern in identifier for identifier in identifiers):
                return BlockMatch("class", index)
        caption = normalize(window.caption)
        for index, pattern in enumerate(self.titles):
            if pattern in caption:
                return BlockMatch("title", index)
        return None


def load_blocklist(path: Path) -> Blocklist:
    """Read a blocklist JSON file. The only I/O in this module; errors propagate.

    The caller must treat a failure as "cannot see": there is no empty fallback list here on
    purpose.
    """
    with path.open("rb") as handle:
        raw = handle.read(MAX_BLOCKLIST_BYTES + 1)
    if len(raw) > MAX_BLOCKLIST_BYTES:
        raise BlocklistError("blocklist file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BlocklistError("blocklist file is not UTF-8") from exc
    return Blocklist.from_json(text)


@dataclass(frozen=True)
class SessionState:
    """What the desktop knows about the session right now.

    ``lock_known`` is False when the lock state could not be determined (no answer from the
    screen saver service or logind). ``idle`` is carried for the caller's benefit; being idle
    does not by itself make Waddle blind.
    """

    locked: bool
    paused: bool
    idle: bool
    lock_known: bool = True


class BlindReason(StrEnum):
    LOCKED = "locked"
    PAUSED = "paused"
    BLOCKED = "blocked"
    OWN_WINDOW = "own-window"
    NO_WINDOW = "no-window"
    UNKNOWN_LOCK = "unknown-lock"


@dataclass(frozen=True)
class Allow:
    """Waddle may look at the window."""


@dataclass(frozen=True)
class Blind:
    """Waddle must not look. ``match`` is set only for ``BlindReason.BLOCKED``."""

    reason: BlindReason
    match: BlockMatch | None = None


def _is_own(window: WindowInfo) -> bool:
    """Own windows by the KWin script's flag, and again by app id (exact, case-insensitive).

    The second test mirrors the capture helper, so a script bug that loses the flag still
    cannot make Waddle look at himself.
    """
    if window.is_own:
        return True
    own = normalize(APP_ID)
    return any(
        normalize(identifier) == own
        for identifier in (window.resource_class, window.resource_name, window.desktop_file)
    )


def gate(window: WindowInfo | None, session: SessionState, blocklist: Blocklist) -> Allow | Blind:
    """Decide whether Waddle may look at ``window``.

    Precedence: locked > lock state unknown > paused > no window > own window > blocked.

    Fail closed: if the desktop could not determine whether the screen is locked
    (``lock_known`` False), the answer is ``Blind(UNKNOWN_LOCK)``. A lock screen that cannot
    be detected must be assumed present; the cost is closed eyes until the session services
    answer, which is the cheap side of that trade.
    """
    if session.locked:
        return Blind(BlindReason.LOCKED)
    if not session.lock_known:
        return Blind(BlindReason.UNKNOWN_LOCK)
    if session.paused:
        return Blind(BlindReason.PAUSED)
    if window is None:
        return Blind(BlindReason.NO_WINDOW)
    if _is_own(window):
        return Blind(BlindReason.OWN_WINDOW)
    match = blocklist.match(window)
    if match is not None:
        return Blind(BlindReason.BLOCKED, match)
    return Allow()
