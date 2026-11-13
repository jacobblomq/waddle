"""Both halves of Waddle in one test process: the real host link and the real brain.

``Rig`` wires ``waddle_desktop.link.BrainLink`` and ``HostToolDispatcher`` (with a small
fake tool backend) to ``waddle_brain.server.LinkServer`` and the real question answerer,
over a real Unix socket. Only the model is fake (``waddle_brain.models.fake``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from waddle_brain.config import BrainConfig, LimitsConfig, RoleConfig
from waddle_brain.models.fake import FakeCall, FakeProvider
from waddle_brain.qa import build_answerer
from waddle_brain.server import LinkServer
from waddle_core.link import AnswerDone
from waddle_desktop.link.client import BrainLink, LinkSocket, LinkState, _unix_connector
from waddle_desktop.link.tools_host import HostToolDispatcher

TOKEN: Final = "5eed" * 16
OTHER_TOKEN: Final = "0bad" * 16
JPEG: Final = b"\xff\xd8\xff\xe0" + b"overview" * 400
PNG: Final = b"\x89PNG\r\n\x1a\n" + b"crop" * 100
# One data block as waddle_core.untrusted.data_block writes it. A body can never hold a line
# that is exactly ">>>": runs of three brackets are broken up before fencing.
BLOCK: Final = re.compile(r"^<<<([a-z_]+)\n(.*?)\n>>>$", re.DOTALL | re.MULTILINE)


class Backends:
    """The host's tool backends, answering from fixed data and counting calls."""

    def __init__(self) -> None:
        self.app_state_result: dict[str, object] = {"mode": "EDIT", "engine": "CYCLES"}
        self.calls: list[str] = []

    async def app_state(self) -> dict[str, object]:
        self.calls.append("app_state")
        return self.app_state_result

    async def system_stats(self, topic: str) -> dict[str, object]:
        self.calls.append("system_stats")
        return {"topic": topic}

    async def man(self, name: str, section: str | None) -> dict[str, object]:
        self.calls.append("man")
        return {"text": name}

    async def tldr(self, name: str) -> dict[str, object]:
        self.calls.append("tldr")
        return {"text": name}


class Sleeper:
    """Stands in for the link's backoff sleep: records each delay and waits until released
    (``BrainLink.kick`` ends the wait too), so no test waits out a real backoff."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self._release.clear()
        await self._release.wait()


class Answer:
    """One request's callbacks, in the order they fired."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def kwargs(self) -> dict[str, Callable[..., None]]:
        return {
            "on_delta": lambda text: self.events.append(("delta", text)),
            "on_done": lambda done: self.events.append(("done", done)),
            "on_error": lambda code: self.events.append(("error", code)),
        }

    @property
    def text(self) -> str:
        return "".join(str(value) for kind, value in self.events if kind == "delta")

    @property
    def ended(self) -> list[tuple[str, object]]:
        return [event for event in self.events if event[0] != "delta"]

    @property
    def done(self) -> AnswerDone:
        ((kind, value),) = self.ended
        assert kind == "done" and isinstance(value, AnswerDone), self.ended
        return value

    @property
    def error(self) -> str:
        ((kind, value),) = self.ended
        assert kind == "error" and isinstance(value, str), self.ended
        return value


class Rig:
    """A private runtime directory, a token, a brain that can be stopped and started again,
    and a host link pointed at it."""

    def __init__(self, root: Path, **limits: float) -> None:
        self.dir = root / "run"
        self.dir.mkdir(mode=0o700)
        self.sock = self.dir / "brain.sock"
        self.token_file = root / "link-token"
        self.token_file.write_text(TOKEN + "\n")
        self.token_file.chmod(0o600)
        self.config = BrainConfig(
            socket_path=self.sock,
            token_path=self.token_file,
            roles={
                "smart_model": RoleConfig(model="test-smart", vision=True),
                "fast_vlm": RoleConfig(model="test-fast", vision=True),
            },
            limits=LimitsConfig.model_validate(limits),
        )
        self.provider = FakeProvider(("smart_model", "fast_vlm"))
        self.server: LinkServer | None = None
        self.backends = Backends()
        self.states: list[LinkState] = []
        self.sleeper = Sleeper()
        self.connects = 0
        self.link = BrainLink(
            self.sock,
            self.token_file,
            client_version="0.3-e2e",
            tools=HostToolDispatcher(self.backends),
            on_state=self.states.append,
            connector=self._connect,
            sleep=self.sleeper,
            jitter=lambda: 0.0,
        )

    async def _connect(self, path: Path) -> LinkSocket:
        self.connects += 1
        return await _unix_connector(path)  # the link's own connector, only counted

    async def start_brain(self, token: str = TOKEN) -> LinkServer:
        answerer = build_answerer(self.config, self.provider)
        self.server = LinkServer(self.config, answerer, token=token, version="0.3-e2e")
        await self.server.start()
        return self.server

    async def stop_brain(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            await server.stop()

    async def kill_brain(self) -> None:
        """The brain dies without a goodbye: its connections are cut, not closed."""
        server = self.server
        assert server is not None and server._server is not None
        for connection in list(server._server.connections):
            connection.transport.abort()
        await self.stop_brain()

    async def up(self) -> None:
        await self.start_brain()
        self.link.start()
        await until(lambda: self.link.state.status == "up")

    def ask(self, text: str = "How do I add a bevel?", **extra: object) -> tuple[str, Answer]:
        answer = Answer()
        options: dict[str, object] = {
            "app": None,
            "window": None,
            "can_see": False,
            "blind_reason": None,
            **answer.kwargs(),
            **extra,
        }
        return self.link.ask(text, **options), answer  # type: ignore[arg-type]

    @property
    def chats(self) -> list[FakeCall]:
        """Every chat the model was given, oldest first."""
        return [call for call in self.provider.calls if call.method == "chat"]

    @property
    def reasons(self) -> list[str]:
        return [state.reason for state in self.states if state.status == "down"]

    async def __aenter__(self) -> Rig:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.link.stop()
        await self.stop_brain()


async def until(condition: Callable[[], object], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.002)


async def settle(seconds: float = 0.05) -> None:
    """Give both ends a moment to act on anything already sent."""
    await asyncio.sleep(seconds)


def blocks(text: str) -> dict[str, str]:
    """The data blocks in a prompt: label -> body."""
    found = BLOCK.findall(text)
    labels = [label for label, _ in found]
    assert len(labels) == len(set(labels)), labels
    return dict(found)


def skeleton(text: str) -> str:
    """A prompt with the body of every data block removed: all that is left is text Waddle
    wrote, or the user typed."""
    return BLOCK.sub(r"<<<\1>>>", text)


def tcp_listeners() -> list[tuple[str, int]]:
    """Every listening TCP socket this process holds, found by walking its open files."""
    found: list[tuple[str, int]] = []
    for entry in Path("/proc/self/fd").iterdir():
        try:
            if not stat.S_ISSOCK(os.fstat(int(entry.name)).st_mode):
                continue
            copy = socket.socket(fileno=os.dup(int(entry.name)))
        except OSError:
            continue  # closed meanwhile (the directory listing's own descriptor, for one)
        with copy:
            if copy.family not in (socket.AF_INET, socket.AF_INET6):
                continue
            if copy.type == socket.SOCK_STREAM and copy.getsockopt(
                socket.SOL_SOCKET, socket.SO_ACCEPTCONN
            ):
                found.append(copy.getsockname()[:2])
    return found


@contextmanager
def captured_logs(*, libraries: int = logging.WARNING) -> Iterator[list[str]]:
    """Every log line of both halves at DEBUG, plus any library's at ``libraries`` or above.

    WARNING for libraries is what the processes run with: nothing in Waddle lowers the root
    logger's level.
    """
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = Collect(level=logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    root = logging.getLogger()
    ours = [logging.getLogger("waddle"), logging.getLogger("waddle_brain")]
    saved = [(log.level, log.propagate, log.handlers[:]) for log in ours]
    root_level = root.level
    for log in ours:
        log.handlers[:] = []
        log.propagate = True
        log.setLevel(logging.DEBUG)
    root.addHandler(handler)
    root.setLevel(libraries)
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        root.setLevel(root_level)
        for log, (level, propagate, handlers) in zip(ours, saved, strict=True):
            log.setLevel(level)
            log.propagate = propagate
            log.handlers[:] = handlers
