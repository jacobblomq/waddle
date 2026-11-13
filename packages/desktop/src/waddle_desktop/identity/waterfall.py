"""The rest of the version waterfall (ARCHITECTURE §4.6, steps 3, 4 and 6; a hook for 5).

``identify()`` has already said which application this is and, when the package metadata
knew it, which version. ``refine_identity`` fills in a missing version from the cheapest
honest source, in this order:

1. packaging metadata of the exact binary (already in ``base``): kept;
2. ``<binary> --version`` in the sandbox, once per binary version (cached);
3. the pack's pattern over the window title;
4. what you told Waddle earlier for this exact binary (cached);
5. OCR of the title bar or About dialog: a hook, wired in Phase 4;
6. otherwise ``needs_user``: the UI asks you to open the About dialog.

Precedence when two sources disagree: metadata of the installed package beats a window
title, because a title is text that documents and plug-ins can influence. The title only
wins over a *weak* base version (one guessed from a file name, confidence below the
title's own 0.7), and only with a full ``major.minor`` that differs. Any such disagreement
is reported in ``Refined.caption_conflict`` either way.

Nothing here raises, runs a program itself, or reads more than one ``stat`` of the binary.
"""

from __future__ import annotations

import asyncio
import math
import posixpath
import re
import stat as stat_module
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from waddle_core.link import AppContext
from waddle_core.untrusted import clean
from waddle_desktop import log
from waddle_desktop.identity.cache import AppIdentity, IdentityCache, cache_key
from waddle_desktop.identity.caption import version_from_caption, version_from_probe_output
from waddle_desktop.identity.packaging import Packaging
from waddle_desktop.identity.packs import Pack
from waddle_desktop.identity.version import Channel, Version, detect_channel, parse_version
from waddle_desktop.sandbox.readonly_exec import ExecResult, Reason

SOURCE_PROBE: Final = "version-probe"
SOURCE_PROBE_NONE: Final = "version-probe-none"  # cached: the probe ran and showed no version
SOURCE_CAPTION: Final = "caption"
SOURCE_USER: Final = "user"
SOURCE_OCR: Final = "ocr"
SOURCE_WEB: Final = "web-app"
CONFIDENCE: Final = {
    SOURCE_PROBE: 0.9,
    SOURCE_CAPTION: 0.7,
    SOURCE_USER: 0.65,
    SOURCE_OCR: 0.5,
    SOURCE_WEB: 0.6,
}
PROBE_SECONDS: Final = 5.0  # the sandbox has its own, shorter limit; this is the backstop
OCR_SECONDS: Final = 10.0
MAX_USER_TEXT: Final = 200
DEFAULT_ABOUT_PATH: Final = ("Help", "About")
# Probing needs the real file on the host. A Flatpak's exe path is a path inside its sandbox,
# and an AppImage would have to be mounted to answer.
_NO_PROBE: Final = frozenset({Packaging.FLATPAK, Packaging.APPIMAGE})
# "Firefox 131", "version 7": a few words and then one whole number.
_TRAILING_NUMBER: Final = re.compile(r"(?:[A-Za-z]{1,20} ){0,4}[vV]?(\d{1,6})")


@dataclass(frozen=True)
class ProbeText:
    """What a version probe printed on a clean run. Untrusted text; may be empty."""

    text: str


# (real executable, flag) -> its output, or None when nothing could be run (no sandbox).
Probe = Callable[[Path, str], Awaitable[ProbeText | None]]
# Step 5, arriving with the OCR service in Phase 4: read the version off the screen as text.
# The text is untrusted and is only ever parsed for a version number.
OcrVersionReader = Callable[[AppIdentity], Awaitable[str | None]]
ExeStat = Callable[[str], tuple[int, int] | None]


@dataclass(frozen=True)
class Refined:
    """The identity after the waterfall, and what the UI should do about a missing version."""

    identity: AppIdentity
    needs_user: bool = False  # ask: "open the About dialog and tell me the version"
    about_path: tuple[str, ...] = ()  # menu labels leading to the About dialog
    version_visible: bool = True  # False for web apps: there is no version to look for
    caption_conflict: bool = False  # the window title names a different major.minor


def exe_stat(path: str) -> tuple[int, int] | None:
    """(size, mtime_ns) of a regular file: what changes when the application is updated."""
    try:
        info = Path(path).stat()
    except (OSError, ValueError):
        return None
    return (info.st_size, info.st_mtime_ns) if stat_module.S_ISREG(info.st_mode) else None


def sandbox_probe(probe_version: Callable[[Path, str], Awaitable[ExecResult]]) -> Probe:
    """Adapt ``ReadOnlyExec.probe_version`` to the ``probe`` argument of ``refine_identity``.

    Only a clean run is an answer: its output is parsed, and "ran, printed no version" is
    remembered for this binary. Everything else becomes None and is tried again another
    time: no sandbox, a refused argument, a missing tool, and also a timeout or a failed
    run, which can be a busy machine rather than a program with nothing to say.
    """

    async def probe(exe: Path, flag: str) -> ProbeText | None:
        result = await probe_version(exe, flag)
        if result.ok and result.reason in (Reason.OK, Reason.TRUNCATED):
            return ProbeText(result.text)
        return None

    return probe


def _with_version(base: AppIdentity, text: str, source: str) -> AppIdentity | None:
    version = parse_version(text)
    if version is None:
        return None
    channel = base.channel if base.channel is not Channel.STABLE else detect_channel(text)
    return replace(
        base,
        version=version,
        version_raw=str(version),
        channel=channel,
        source=source,
        confidence=CONFIDENCE[source],
    )


def _key(kind: str, base: AppIdentity, stat: ExeStat) -> str | None:
    """A cache key in the existing scheme, in its own namespace so it never meets the keys
    ``identify()`` writes. Without a stat of the binary there is no key and nothing is cached."""
    if not base.exe.startswith("/") or base.packaging is Packaging.FLATPAK:
        return None  # a Flatpak's exe path names a file inside its sandbox, not on the host
    return cache_key(f"{kind}:{base.app_id}", base.exe, exe_stat=stat(base.exe))


def _may_probe(base: AppIdentity, pack: Pack) -> bool:
    if pack.version_probe is None or base.packaging in _NO_PROBE:
        return False
    return base.exe.startswith("/") and posixpath.basename(base.exe).lower() in pack.match.binaries


async def _probe_step(
    base: AppIdentity,
    pack: Pack,
    probe: Probe,
    cache: IdentityCache | None,
    stat: ExeStat,
    seconds: float,
) -> AppIdentity | None:
    key = _key("probe", base, stat)
    if key is None or pack.version_probe is None:
        return None  # the binary is not a file we can see: nothing to run
    hit = cache.get(key) if cache is not None else None
    if hit is not None:
        if hit.version is None:
            return None
        return _with_version(base, str(hit.version), SOURCE_PROBE)
    try:
        async with asyncio.timeout(seconds):
            output = await probe(Path(base.exe), pack.version_probe.flag)
    except Exception:  # includes the timeout; a broken probe is a skipped step
        output = None
    if output is None:
        return None
    text = version_from_probe_output(pack, output.text)
    found = _with_version(base, text, SOURCE_PROBE) if text else None
    if cache is not None:
        cache.put(
            key, found or replace(base, version=None, version_raw="", source=SOURCE_PROBE_NONE)
        )
    return found


def _differs(a: Version, b: Version) -> bool:
    return (a.major, a.minor) != (b.major, b.minor)


async def _refine(
    base: AppIdentity,
    caption: str,
    pack: Pack | None,
    probe: Probe | None,
    cache: IdentityCache | None,
    ocr: OcrVersionReader | None,
    stat: ExeStat,
    probe_seconds: float,
) -> Refined:
    if pack is not None and pack.web_app is not None:
        web = replace(
            base,
            app_id=pack.id,
            name=pack.name,
            packaging=Packaging.UNKNOWN,
            package_id="",
            version=None,
            version_raw="",
            channel=Channel.STABLE,
            edition=None,
            source=SOURCE_WEB,
            confidence=CONFIDENCE[SOURCE_WEB],
        )
        return Refined(web, version_visible=False)

    titled = None
    if pack is not None:
        shown = version_from_caption(pack, caption)
        titled = _with_version(base, shown, SOURCE_CAPTION) if shown else None

    if base.version is not None:
        conflict = (
            titled is not None
            and titled.version is not None
            and _differs(titled.version, base.version)
        )
        if conflict and titled is not None and base.confidence < CONFIDENCE[SOURCE_CAPTION]:
            return Refined(titled, caption_conflict=True)
        return Refined(base, caption_conflict=conflict)

    if pack is not None and probe is not None and _may_probe(base, pack):
        probed = await _probe_step(base, pack, probe, cache, stat, probe_seconds)
        if probed is not None:
            return Refined(probed)

    if titled is not None:
        return Refined(titled)

    key = _key("user", base, stat) if cache is not None else None
    told = cache.get(key) if cache is not None and key is not None else None
    if told is not None and told.version is not None:
        remembered = _with_version(base, str(told.version), SOURCE_USER)
        if remembered is not None:
            return Refined(remembered)

    if ocr is not None:
        try:
            async with asyncio.timeout(OCR_SECONDS):
                seen = await ocr(base)
        except Exception:
            seen = None
        read = (
            _with_version(base, seen[:MAX_USER_TEXT], SOURCE_OCR) if isinstance(seen, str) else None
        )
        if read is not None:
            return Refined(read)

    about = pack.about_path if pack is not None and pack.about_path else DEFAULT_ABOUT_PATH
    return Refined(base, needs_user=True, about_path=about)


async def refine_identity(
    base: AppIdentity,
    *,
    caption: str,
    pack: Pack | None,
    probe: Probe | None,
    cache: IdentityCache | None,
    ocr: OcrVersionReader | None = None,
    stat: ExeStat = exe_stat,
    probe_seconds: float = PROBE_SECONDS,
) -> Refined:
    """Fill in the version of ``base`` (see the module text for order and precedence).

    ``caption`` is the window title (untrusted). ``pack`` is ``PackIndex.find(...)`` for this
    window. ``probe`` runs one allowlisted version flag in the sandbox, or is None when there
    is no sandbox; it is only called for the pack's own binary, and its answer is cached per
    binary version. ``ocr`` is the step-5 hook. Never raises; when no step finds a version
    the result has ``needs_user`` set and the menu path to the About dialog.
    """
    try:
        refined = await _refine(base, caption, pack, probe, cache, ocr, stat, probe_seconds)
    except Exception:  # everything read here is untrusted: a bug must not reach the UI
        log.warning("identity_refine_failed")
        return Refined(base, needs_user=base.version is None, about_path=DEFAULT_ABOUT_PATH)
    log.event(
        "identity_refined",
        source=refined.identity.source,
        has_version=refined.identity.version is not None,
        needs_user=refined.needs_user,
        conflict=refined.caption_conflict,
    )
    return refined


def apply_user_version(
    identity: AppIdentity,
    text: str,
    *,
    cache: IdentityCache | None = None,
    stat: ExeStat = exe_stat,
) -> AppIdentity | None:
    """The identity with the version you typed or read out, or None if there is none in it.

    Accepts "5.3", "Blender 5.3.1", "version 21.0.3 build 7", and a lone whole number after a
    few words ("Firefox 131"). With a cache the answer is remembered for this exact binary,
    so the question is not asked again until the application is updated.
    """
    text = clean(text[:MAX_USER_TEXT], MAX_USER_TEXT).strip()
    answered = _with_version(identity, text, SOURCE_USER)
    if answered is None:
        number = _TRAILING_NUMBER.fullmatch(text)
        answered = _with_version(identity, number.group(1), SOURCE_USER) if number else None
    if answered is None:
        return None
    if cache is not None:
        key = _key("user", identity, stat)
        if key is not None:
            cache.put(key, answered)
    return answered


def _capped(text: object, limit: int) -> str:
    """One clean line of at most ``limit`` characters; "" for anything that is not text."""
    if not isinstance(text, str):
        return ""
    return " ".join(clean(text[: limit * 4], limit).split())


def to_app_context(identity: AppIdentity, *, pack: Pack | None = None) -> AppContext:
    """The identity as the link's ``AppContext``: cleaned, capped and schema-validated.

    With ``pack`` the application's display name ("DaVinci Resolve") replaces the binary's
    name. The executable path and package id are not part of the context and stay on the
    host. Anything that will not validate gives an empty context instead of an error.
    """
    confidence = identity.confidence
    if not isinstance(confidence, int | float) or not math.isfinite(confidence):
        confidence = 0.0
    version = identity.version
    known = version is not None or identity.source == SOURCE_WEB
    try:
        return AppContext(
            app_id=_capped(identity.app_id, 128),
            name=_capped(pack.name if pack is not None else identity.name, 64),
            version=_capped(str(version), 32) if version is not None else None,
            version_source=_capped(identity.source, 32) if known else "unknown",
            packaging=_capped(str(identity.packaging), 16) or "unknown",
            channel=_capped(str(identity.channel), 16) or "stable",
            edition=_capped(identity.edition, 32) or None,
            confidence=min(1.0, max(0.0, float(confidence))),
        )
    except (ValidationError, ValueError, TypeError):
        return AppContext()
