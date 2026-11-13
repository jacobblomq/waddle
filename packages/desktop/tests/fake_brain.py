"""A small, scriptable stand-in for waddle-brain: a real WebSocket server on a Unix socket.

It speaks the link protocol from ``waddle_core.link`` and records everything the host sends,
in order, so tests can check both what was said and when.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from websockets.asyncio.server import Server, ServerConnection, unix_serve
from websockets.exceptions import ConnectionClosed

from waddle_core.link import (
    MAX_IMAGE_BYTES,
    Ask,
    Hello,
    HostMessage,
    ModelStatus,
    Refused,
    Welcome,
    decode_host,
    decode_image,
    encode,
)

Handshake = Literal["welcome", "refuse", "garbage", "silent", "drop"]
MODELS = (ModelStatus(role="smart_model", model="test-model", available=True, loaded=False),)


class Session:
    """One accepted connection, as a script sees it."""

    def __init__(self, ws: ServerConnection):
        self._ws = ws

    async def send(self, message: object) -> None:
        await self._ws.send(encode(message))  # type: ignore[arg-type]

    async def send_raw(self, data: str | bytes) -> None:
        await self._ws.send(data)

    async def drop(self) -> None:
        """Cut the connection without a closing handshake, as a crashed brain would."""
        self._ws.transport.abort()


OnAsk = Callable[[Session, Ask], Awaitable[None]]


class FakeBrain:
    """Serve ``path``. ``handshake`` picks the reply to hello; ``on_ask`` scripts each ask."""

    def __init__(
        self,
        path: Path,
        token: str,
        *,
        handshake: Handshake = "welcome",
        on_ask: OnAsk | None = None,
        on_welcome: Callable[[Session], Awaitable[None]] | None = None,
    ):
        self.path = path
        self.token = token
        self.handshake: Handshake = handshake
        self.on_ask = on_ask
        self.on_welcome = on_welcome  # runs right after each welcome
        self.connections = 0
        self.hellos: list[Hello] = []
        # Host messages in arrival order; an image is ("image", image_id, mime, data).
        self.received: list[HostMessage | tuple[str, str, str, bytes]] = []
        self.sessions: list[Session] = []
        self._server: Server | None = None

    async def start(self) -> None:
        old_mask = os.umask(0o177)
        try:
            self._server = await unix_serve(
                self._serve, str(self.path), max_size=MAX_IMAGE_BYTES + 1024
            )
        finally:
            os.umask(old_mask)

    async def stop(self) -> None:
        """Go away like a stopped container: connections cut, socket file removed."""
        server, self._server = self._server, None
        if server is None:
            return
        for session in self.sessions:
            await session.drop()
        self.sessions.clear()
        server.close()
        await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def of_type[T](self, kind: type[T]) -> list[T]:
        return [m for m in self.received if isinstance(m, kind)]

    async def _serve(self, ws: ServerConnection) -> None:
        self.connections += 1
        session = Session(ws)
        with contextlib.suppress(ConnectionClosed):
            if not await self._greet(ws):
                return
            self.sessions.append(session)
            if self.on_welcome is not None:
                await self.on_welcome(session)
            async for raw in ws:
                if isinstance(raw, bytes):
                    self.received.append(("image", *decode_image(raw)))
                    continue
                message = decode_host(raw)
                self.received.append(message)
                if isinstance(message, Ask) and self.on_ask is not None:
                    await self.on_ask(session, message)

    async def _greet(self, ws: ServerConnection) -> bool:
        raw = await ws.recv()
        hello = decode_host(raw)
        assert isinstance(hello, Hello)
        self.hellos.append(hello)
        match self.handshake:
            case "silent":
                await ws.wait_closed()  # say nothing, for as long as the host stays
            case "drop":
                ws.transport.abort()
            case "garbage":
                await ws.send("{not json")
            case "refuse":
                await ws.send(encode(Refused(id=hello.id, reason="busy")))
            case "welcome" if hello.token != self.token:
                await ws.send(encode(Refused(id=hello.id, reason="bad_token")))
            case "welcome":
                await ws.send(encode(Welcome(id=hello.id, server_version="test", models=MODELS)))
                return True
        return False
