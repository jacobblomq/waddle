"""A version from untrusted text by a pack's pattern (ARCHITECTURE §4.6, steps 3 and 4).

Window titles contain file names and page titles chosen by anyone, and a probed program
prints what it likes. So the text is capped, only the pack's own pattern is applied to it,
and the only thing that can come out is a normalized version string such as ``"5.3.1"``.
"""

from __future__ import annotations

import re
from typing import Final

from waddle_desktop.identity.packs import MAX_CAPTION_CHARS, MAX_PROBE_CHARS, Pack
from waddle_desktop.identity.version import parse_version

__all__ = ["MAX_PROBE_CHARS", "version_from_caption", "version_from_probe_output"]

_MAX_VERSION_CHARS: Final = 48


def _extract(regex: re.Pattern[str], text: str, limit: int, *, dotted: bool) -> str | None:
    match = regex.search(text[:limit])
    if match is None:
        return None
    if len(text) > limit and match.end() == limit:
        # The match runs up to the cut, so it may rest on text that is not there: "4.25"
        # must not read as "4.2", and "$" must not take the cut for the end of the title.
        return None
    raw = (match.group("version") or "")[:_MAX_VERSION_CHARS]
    if dotted and "." not in raw:
        return None
    version = parse_version(raw)
    return None if version is None else str(version)


def version_from_caption(pack: Pack, caption: str) -> str | None:
    """The version the window title shows, normalized, or None.

    Only the first 200 characters are looked at, and only a ``major.minor`` (or longer)
    number counts: a lone "5" in a title is too weak to be called a version.
    """
    if pack.caption_version is None or not isinstance(caption, str):
        return None
    return _extract(pack.caption_version.regex, caption, MAX_CAPTION_CHARS, dotted=True)


def version_from_probe_output(pack: Pack, output: str) -> str | None:
    """The version in what ``<binary> <flag>`` printed, normalized, or None."""
    if pack.version_probe is None or not isinstance(output, str):
        return None
    return _extract(pack.version_probe.regex, output, MAX_PROBE_CHARS, dotted=False)
