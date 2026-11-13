"""Shared fakes for the assist tests: a helper eye, a sandbox, windows and identities."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from waddle_core.config import WatchingConfig
from waddle_core.privacy import Blocklist
from waddle_core.watching import CaptureScheduler, WatchPolicy
from waddle_core.window import Geometry, WindowInfo
from waddle_desktop.identity.cache import UNKNOWN_IDENTITY, AppIdentity
from waddle_desktop.identity.packaging import Packaging
from waddle_desktop.identity.packs import PackIndex, load_packs
from waddle_desktop.identity.version import parse_version
from waddle_desktop.sandbox.readonly_exec import ExecResult, Reason, SandboxStatus
from waddle_desktop.sensors.capture_client import CaptureResult, Frame, Hello
from waddle_desktop.sensors.helper_proc import HelperUnavailableError
from waddle_desktop.sensors.senses import EyesState, Senses
from waddle_desktop.sensors.session import SessionSnapshot

PACKS_DIR = Path(__file__).parent / "fixtures" / "apps" / "packs"
UNLOCKED = SessionSnapshot(locked=False, lock_known=True, dnd=False, idle=False)
LOCKED = replace(UNLOCKED, locked=True)
G = Geometry(0, 0, 800, 600)
U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
JPEG = b"\xff\xd8" + b"j" * 4000
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 900
HELLO = Hello("0.2.0", 1, True, 4, "restricted", True, True, False)
OWN_ID = "io.github.jacobblomq.Waddle"


def win(
    uuid: str = U1,
    *,
    cls: str = "GoodApp",
    caption: str = "scene - Good 5.3",
    pid: int = 10,
    own: bool = False,
) -> WindowInfo:
    return WindowInfo(uuid, caption, cls, cls, cls, pid, G, G, "DP-1", 1.0, False, False, own)


def frame(overview: bytes = JPEG, crop: bytes = PNG) -> Frame:
    return Frame("0" * 16, 1.0, 1600, 1200, overview, (800, 600), crop, (0, 0, 512, 512), 7, 0)


def ident(
    name: str = "good",
    *,
    exe: str = "/usr/bin/good",
    version: str | None = "5.3.1",
    confidence: float = 0.95,
    packaging: Packaging = Packaging.DEB,
    source: str = "dpkg",
) -> AppIdentity:
    return replace(
        UNKNOWN_IDENTITY,
        app_id=name,
        name=name,
        exe=exe,
        packaging=packaging,
        version=parse_version(version) if version else None,
        version_raw=version or "",
        source=source,
        confidence=confidence,
    )


def fixture_packs() -> PackIndex:
    return load_packs(PACKS_DIR)


class FakeEye:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.results: list[CaptureResult | Exception] = []
        self.closed = False
        self.gate: asyncio.Event | None = None

    async def hello(self) -> Hello:
        self.calls.append(("hello",))
        return HELLO

    async def set_blind(self, on: bool) -> None:
        self.calls.append(("blind", on))

    async def forget(self) -> None:
        self.calls.append(("forget",))

    async def capture_window(
        self, uuid: str, *, popups: Iterable[str] = (), force: bool = False
    ) -> CaptureResult:
        self.calls.append(("capture", uuid, tuple(popups), force))
        if self.gate is not None:
            await self.gate.wait()
        result = self.results.pop(0) if self.results else frame()
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True

    def captures(self) -> list[tuple[object, ...]]:
        return [c for c in self.calls if c[0] == "capture"]


class SensesRig:
    """Real ``Senses`` over a fake helper. ``identity`` is what ``identify`` returns."""

    def __init__(self) -> None:
        self.eye = FakeEye()
        self.started = 0
        self.states: list[EyesState] = []
        self.frames: list[Frame | None] = []
        self.unavailable: str | None = None
        self.identity: AppIdentity = ident()
        self.identified = 0
        self.senses = Senses(
            blocklist=Blocklist(classes=("keepassxc",), titles=("private browsing",)),
            scheduler=CaptureScheduler(WatchPolicy.from_config(WatchingConfig())),
            eye_factory=self._factory,
            identify=self._identify,
            on_state=self.states.append,
            on_frame=self.frames.append,
        )

    def _identify(self, _window: WindowInfo) -> AppIdentity:
        self.identified += 1
        return self.identity

    async def _factory(self) -> FakeEye:
        if self.unavailable:
            raise HelperUnavailableError(self.unavailable)
        self.started += 1
        return self.eye

    async def focus(self, window: WindowInfo, *, session: SessionSnapshot = UNLOCKED) -> None:
        """Unlock (or not), focus ``window`` and wait until it has been identified."""
        self.senses.session_changed(session)
        self.senses.window_activated(window)
        await until(lambda: self.senses.state.identity is not None)


class FakeExec:
    """Stands in for ``ReadOnlyExec``: scripted results, every call recorded."""

    def __init__(self, *, available: bool = True, reason: str = "ok") -> None:
        self.available = available
        self.reason = reason
        self.calls: list[tuple[str, ...]] = []
        self.results: list[ExecResult] = []
        self.gate: asyncio.Event | None = None

    async def status(self) -> SandboxStatus:
        self.calls.append(("status",))
        return SandboxStatus(self.available, self.reason)

    async def _next(self, *call: str) -> ExecResult:
        self.calls.append(call)
        if self.gate is not None:
            await self.gate.wait()
        return self.results.pop(0) if self.results else ExecResult(True, "text", Reason.OK, 1)

    async def man(self, name: str, section: str | None = None) -> ExecResult:
        return await self._next("man", name, section or "")

    async def tldr(self, name: str) -> ExecResult:
        return await self._next("tldr", name)

    async def probe_version(self, exe: Path, flag: str) -> ExecResult:
        return await self._next("probe", str(exe), flag)

    def of(self, verb: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == verb]


def result(text: str = "", reason: Reason = Reason.OK) -> ExecResult:
    return ExecResult(reason in (Reason.OK, Reason.TRUNCATED), text, reason, 1)


async def until(condition: Callable[[], object], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def settle(turns: int = 30) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


@contextmanager
def captured_logs() -> Iterator[list[str]]:
    """Every line the desktop logger writes while the block runs."""
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    logger = logging.getLogger("waddle")
    handler = Collect()
    level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
