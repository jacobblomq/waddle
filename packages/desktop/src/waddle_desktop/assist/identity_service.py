"""Which app is in front, and which version: the waterfall, kept current per window.

The senses already ran ``identify()`` for the focused window (steps 1-2 of ARCHITECTURE
§4.6). This service takes that result, finds the app's pack and runs the remaining steps
(``refine_identity``: sandboxed version probe, title pattern, remembered answer), then keeps
the outcome for that window.

Rules it keeps:

- Refinement never holds up a focus change: it runs as one background task, one window at a
  time, and a result for a window that is no longer the one in front (or whose title or
  identity changed meanwhile) is dropped.
- Nothing is worked out or handed on for a window the privacy gate does not allow: no pack
  matching on its title, no probe of its binary, no ``AppContext``.
- A version is passed on only when it is trustworthy enough (``VERSION_CONFIDENCE``); a guess
  is sent as "unknown" so the answer says so instead of sounding sure.
- A version probe that timed out is tried again later, not remembered as "has no version".

Everything runs on the I/O loop. Logs carry event names and booleans only.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, NamedTuple, Protocol

from waddle_core.link import AppContext
from waddle_core.privacy import BlindReason
from waddle_desktop import log
from waddle_desktop.assist.shared_exec import SharedExec
from waddle_desktop.identity import waterfall
from waddle_desktop.identity.cache import AppIdentity, IdentityCache
from waddle_desktop.identity.packs import Pack, PackIndex
from waddle_desktop.identity.waterfall import Probe, ProbeText, Refined
from waddle_desktop.sandbox.readonly_exec import Reason
from waddle_desktop.sensors.senses import EyesState

VERSION_CONFIDENCE: Final = 0.6  # below this a version is not passed on as fact
PROBE_RETRY_SECONDS: Final = 600.0  # a probe that timed out is not tried again before this
MAX_WINDOWS: Final = 64
_MAX_REMEMBERED: Final = 64


class Current(NamedTuple):
    """What is known about the app in front. ``app`` is None when nothing may be said."""

    app: AppContext | None
    pack: Pack | None
    needs_user: bool  # ask: "open the About dialog and tell me the version"
    about_path: tuple[str, ...]  # menu labels leading to the About dialog


NOTHING: Final = Current(None, None, False, ())


class Refine(Protocol):
    def __call__(
        self,
        base: AppIdentity,
        *,
        caption: str,
        pack: Pack | None,
        probe: Probe | None,
        cache: IdentityCache | None,
    ) -> Awaitable[Refined]: ...


@dataclass(frozen=True)
class _Key:
    """What a refinement was worked out for. If any of it changes, the result is stale."""

    uuid: str
    pid: int
    identity: AppIdentity
    caption: str  # untrusted; never logged


@dataclass(frozen=True)
class _Entry:
    key: _Key
    refined: Refined
    pack: Pack | None


def _bounded[K, V](store: dict[K, V], key: K, value: V, limit: int) -> None:
    store.pop(key, None)
    store[key] = value
    while len(store) > limit:
        del store[next(iter(store))]


class IdentityService:
    def __init__(
        self,
        *,
        eyes: Callable[[], EyesState],
        packs: PackIndex,
        cache: IdentityCache | None,
        sandbox: SharedExec | None,
        refine: Refine = waterfall.refine_identity,
        on_change: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        stat: waterfall.ExeStat = waterfall.exe_stat,
    ):
        """``eyes`` returns the senses' current state. ``packs.errors`` is for the doctor.

        ``on_change`` is called on the loop whenever ``current()`` may have a better answer
        than before (a refinement finished, or you told Waddle the version).
        """
        self.packs: Final = packs
        self._eyes = eyes
        self._cache = cache
        self._sandbox = sandbox
        self._refine = refine
        self._on_change = on_change
        self._clock = clock
        self._stat = stat
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._wanted: _Key | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._probe_memo: dict[tuple[str, str, tuple[int, int] | None], ProbeText] = {}
        self._probe_wait: dict[str, float] = {}
        self._told: dict[tuple[str, str, tuple[int, int] | None], AppIdentity] = {}

    # ---- inputs (call on the loop) -------------------------------------------------------

    def on_eyes(self, state: EyesState) -> None:
        """The senses' state changed. Starts a refinement if the window in front needs one."""
        if self._stopped:
            return
        if state.blind is BlindReason.BLOCKED and state.window is not None:
            self._entries.pop(state.window.uuid, None)  # keep nothing about a private window
        key = _key(state)
        if key is None or self._known(key) is not None:
            self._wanted = None  # anything still in flight is now stale
            return
        self._wanted = key
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._work())

    def apply_user_version(self, text: str) -> bool:
        """You read out the version from the About dialog. True if a version was found in it.

        Only takes effect while Waddle is asking (``current().needs_user``) about the window
        in front; the answer is remembered for this exact binary.
        """
        key = _key(self._eyes())
        entry = self._known(key) if key is not None else None
        if key is None or entry is None or not entry.refined.needs_user:
            return False
        answered = waterfall.apply_user_version(
            key.identity, text, cache=self._cache, stat=self._stat
        )
        if answered is None:
            return False
        _bounded(self._told, self._told_key(key.identity), answered, _MAX_REMEMBERED)
        self._store(_Entry(key, Refined(answered), entry.pack))
        log.event("identity_user_version")
        return True

    async def stop(self) -> None:
        self._stopped = True
        self._wanted = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ---- the answer ----------------------------------------------------------------------

    def current(self) -> Current:
        """The app in front, for a question or the UI. Never blocks and never raises."""
        key = _key(self._eyes())
        if key is None:
            return NOTHING
        entry = self._known(key)
        if entry is not None:
            refined = entry.refined
            return Current(
                _context(refined.identity, entry.pack),
                entry.pack,
                refined.needs_user,
                refined.about_path,
            )
        # Not refined yet: what identify() said, and the pack by app alone (not by title).
        pack = self._find(key.identity, "")
        return Current(_context(key.identity, pack), pack, False, ())

    @property
    def settled(self) -> bool:
        """Nothing is left to work out for the window in front (or nothing may be)."""
        key = _key(self._eyes())
        return key is None or self._known(key) is not None

    # ---- refinement ----------------------------------------------------------------------

    def _known(self, key: _Key) -> _Entry | None:
        entry = self._entries.get(key.uuid)
        return entry if entry is not None and entry.key == key else None

    def _store(self, entry: _Entry) -> None:
        self._entries.pop(entry.key.uuid, None)
        self._entries[entry.key.uuid] = entry
        while len(self._entries) > MAX_WINDOWS:
            self._entries.popitem(last=False)
        if self._on_change is not None and not self._stopped:
            try:
                self._on_change()
            except Exception as exc:
                log.warning("identity_callback_failed", kind=type(exc).__name__)

    def _find(self, identity: AppIdentity, caption: str) -> Pack | None:
        try:
            return self.packs.find(identity, caption=caption)
        except Exception:  # identity fields and titles are untrusted text
            return None

    async def _work(self) -> None:
        """Refine whatever window is wanted, one at a time, until nothing is."""
        while not self._stopped:
            key = self._wanted
            if key is None or self._known(key) is not None:
                return
            pack = self._find(key.identity, key.caption)
            try:
                refined = await self._refine(
                    key.identity,
                    caption=key.caption,
                    pack=pack,
                    probe=self._probe if self._sandbox is not None else None,
                    cache=self._cache,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("identity_refine_error", kind=type(exc).__name__)
                refined = Refined(key.identity)
            if key != self._wanted:
                # The window, its title or its identity changed while this was running.
                log.event("identity_stale_dropped")
                continue
            self._wanted = None
            self._store(_Entry(key, self._remembered(key.identity, refined), pack))

    def _told_key(self, identity: AppIdentity) -> tuple[str, str, tuple[int, int] | None]:
        return (identity.app_id, identity.exe, self._stat(identity.exe))

    def _remembered(self, base: AppIdentity, refined: Refined) -> Refined:
        """What you told Waddle earlier in this session, where the cache could not keep it."""
        if not refined.needs_user:
            return refined
        told = self._told.get(self._told_key(base))
        if told is None or told.version is None:
            return refined
        return Refined(
            replace(
                base,
                version=told.version,
                version_raw=told.version_raw,
                channel=told.channel,
                source=told.source,
                confidence=told.confidence,
            )
        )

    async def _probe(self, exe: Path, flag: str) -> ProbeText | None:
        """``refine_identity``'s probe. None means "nothing learned; try another time"."""
        sandbox_owner = self._sandbox
        if sandbox_owner is None:
            return None
        memo_key = (str(exe), flag, self._stat(str(exe)))
        known = self._probe_memo.get(memo_key)
        if known is not None:
            return known
        if self._clock() < self._probe_wait.get(str(exe), 0.0):
            return None
        sandbox = await sandbox_owner.ready()
        if sandbox is None:
            return None
        result = await sandbox.probe_version(exe, flag)
        if result.reason in (Reason.UNAVAILABLE, Reason.INVALID):
            return None
        if result.reason is Reason.TIMEOUT:
            # A slow start is not "this program has no version": do not let it be cached.
            _bounded(
                self._probe_wait,
                str(exe),
                self._clock() + PROBE_RETRY_SECONDS,
                _MAX_REMEMBERED,
            )
            return None
        text = ProbeText(result.text if result.ok else "")
        _bounded(self._probe_memo, memo_key, text, _MAX_REMEMBERED)
        return text


def _key(state: EyesState) -> _Key | None:
    """What to identify, or None when the gate forbids the window or nothing is known yet."""
    window, identity = state.window, state.identity
    if state.blind is not None or window is None or identity is None:
        return None
    return _Key(window.uuid, window.pid, identity, window.caption)


def _context(identity: AppIdentity, pack: Pack | None) -> AppContext | None:
    """The link's app context: None without a name, and no version that is only a guess."""
    context = waterfall.to_app_context(identity, pack=pack)
    if not context.name:
        return None
    if context.version is not None and context.confidence < VERSION_CONFIDENCE:
        return context.model_copy(update={"version": None, "version_source": "unknown"})
    return context
