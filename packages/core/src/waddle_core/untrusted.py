"""Untrusted text: anything Waddle did not write and you did not type (ARCHITECTURE §5.6).

Screen text, window titles, command output, file metadata, web pages. It is only ever placed
in a labelled data block, cleaned and capped, and never in an instruction position.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

OPEN: Final = "<<<"
CLOSE: Final = ">>>"
_LABEL: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Bidi controls, zero-width characters, BOM, tag characters, variation selectors, and
# letters/marks that render as nothing (grapheme joiner, Khmer inherent vowels, Hangul and
# Khitan fillers).
_INVISIBLE: Final = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
    "\u2060-\u2069\u3164\ufe00-\ufe0f\ufeff\uffa0\U00016fe4"
    "\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)
# Unicode line and paragraph separators break a line like "\n" does; make that visible.
_LINE_BREAKS: Final = re.compile("[\u2028\u2029]")
# Runs of three or more angle brackets, counting the full-width and small look-alikes that
# normalize (NFKC) to "<" and ">".
_FENCE_RUN: Final = re.compile("[<\uff1c\ufe64]{3,}|[>\uff1e\ufe65]{3,}")
ELLIPSIS: Final = "…"


def clean(text: str, limit: int) -> str:
    """Printable text of at most ``limit`` characters. Newlines and tabs are kept.

    Control characters, bidi overrides and invisible characters are removed (they can hide
    or reorder what a person or a model reads); a cut is marked with an ellipsis.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    text = _LINE_BREAKS.sub("\n", _INVISIBLE.sub("", text))
    kept = [
        ch
        for ch in text
        if ch in "\n\t" or unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Co", "Cn"}
    ]
    out = "".join(kept)
    return out if len(out) <= limit else out[: limit - 1] + ELLIPSIS


def _spaced_pairs(run: re.Match[str]) -> str:
    chars = run.group()
    return " ".join(chars[i : i + 2] for i in range(0, len(chars), 2))


def data_block(label: str, text: str, limit: int) -> str:
    """``text`` as a fenced, labelled block that its content cannot close or imitate.

    Every run of three or more angle brackets in the content gets a space after each pair
    (``>>>`` becomes ``>> >``), so no input can end the block early or open a second one with
    a more trusted-looking label. ``limit`` caps the text before those spaces are added.
    """
    if not _LABEL.fullmatch(label):
        raise ValueError("bad block label")
    body = _FENCE_RUN.sub(_spaced_pairs, clean(text, limit))
    return f"{OPEN}{label}\n{body}\n{CLOSE}"
