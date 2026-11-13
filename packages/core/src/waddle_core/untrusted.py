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
# Bidi controls, zero-width characters, BOM, tag characters, variation selectors.
_INVISIBLE: Final = re.compile("[­؜᠎​-‏‪-‮⁠-⁩﻿︀-️\U000e0000-\U000e007f\U000e0100-\U000e01ef]")
ELLIPSIS: Final = "…"


def clean(text: str, limit: int) -> str:
    """Printable text of at most ``limit`` characters. Newlines and tabs are kept.

    Control characters, bidi overrides and invisible characters are removed (they can hide
    or reorder what a person or a model reads); a cut is marked with an ellipsis.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    text = _INVISIBLE.sub("", text)
    kept = [
        ch
        for ch in text
        if ch in "\n\t" or unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Co", "Cn"}
    ]
    out = "".join(kept)
    return out if len(out) <= limit else out[: limit - 1] + ELLIPSIS


def data_block(label: str, text: str, limit: int) -> str:
    """``text`` as a fenced, labelled block that its content cannot close or imitate.

    The fence markers are broken up inside the content, so no input can end the block early
    or open a second one with a more trusted-looking label.
    """
    if not _LABEL.match(label):
        raise ValueError("bad block label")
    body = clean(text, limit).replace(OPEN, "<< <").replace(CLOSE, ">> >")
    return f"{OPEN}{label}\n{body}\n{CLOSE}"
