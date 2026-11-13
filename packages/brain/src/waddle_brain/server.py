"""The brain's link server (ARCHITECTURE §5.2, D-034).

One WebSocket over a Unix socket; there is no TCP listener anywhere. The host must present
the link token in its first message, only one host is served at a time, and every message is
size-checked and schema-checked before anything acts on it. Images stay in memory until
their ask claims them. Nothing the host, a tool or a model said is ever logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import socket
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from pydantic import ValidationError
from websockets.asyncio.server import Server, ServerConnection, unix_serve
from websockets.exceptions import ConnectionClosed

from waddle_brain import log
from waddle_brain.config import BrainConfig
from waddle_brain.contracts import Answerer, AskContext, ProviderError
from waddle_brain.requests import ImageStore, OpenRequest, Outgoing
from waddle_core.link import (
    IMAGE_TTL_SECONDS,
    MAX_IMAGE_BYTES,
    AnswerDone,
    AnswerError,
    Ask,
    Cancel,
    ErrorCode,
    Hello,
    LinkError,
    ModelStatus,
    Ping,
    Pong,
    Prewarm,
    Refused,
    Role,
    State,
    Timings,
    ToolResult,
    Welcome,
    decode_host,
    decode_image,
    encode,
)

HELLO_TIMEOUT_S: Final = 5.0
STATUS_TIMEOUT_S: Final = 2.0  # how long the welcome waits for model status
MAX_HANDSHAKES: Final = 8  # connections that have not said a valid hello yet
_MAX_MESSAGE_BYTES: Final = MAX_IMAGE_BYTES + 512 + 4  # the largest binary image message
_NO_ID: Final = "0"  # id for a refusal when the peer gave no usable one

# The websockets library logs through its own logger; keep it off our allowlisted stream.
_library_log = logging.getLogger("waddle_brain.websockets")
_library_log.addHandler(logging.NullHandler())
_library_log.propagate = False
_library_log.setLevel(logging.CRITICAL + 1)


class LinkServerError(Exception):
    """The server cannot start. ``code`` is safe to log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _bind(path: Path) -> socket.socket:
    """A Unix socket bound at ``path`` with mode 0600. Only a dead socket is replaced."""
    if not path.parent.is_dir():
        raise LinkServerError("no_socket_dir")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(mode):
            raise LinkServerError("path_is_not_a_socket")
        if _is_live(path):
            raise LinkServerError("socket_in_use")
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # UNVERIFIED (spike S3.4, proposed): with the runtime directory bind-mounted into the
    # container, the host user can only connect to a 0600 socket if the brain runs under the
    # same uid (user namespace mapping). Not tested: no container runtime here.
    old_mask = os.umask(0o177)
    try:
        sock.bind(str(path))
    except OSError:
        sock.close()
        raise LinkServerError("bind_failed") from None
    finally:
        os.umask(old_mask)
    return sock


def _is_live(path: Path) -> bool:
    """True if something is accepting connections on the socket at ``path``."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True  # cannot tell: leave it alone
    finally:
        probe.close()
    return True


class LinkServer:
    """Serves the host on ``config.socket_path`` and hands its asks to ``answerer``."""

    def __init__(
        self,
        config: BrainConfig,
        answerer: Answerer,
        *,
        token: str,
        version: str,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._answerer = answerer
        self._token = token.encode()
        self._version = version[:32]
        self._clock = clock
        self._server: Server | None = None
        self._active: _Connection | None = None
        self._handshakes = 0
        self._prewarm: asyncio.Task[None] | None = None

    @property
    def sockets(self) -> tuple[socket.socket, ...]:
        """The listening sockets (for checks that there is no TCP listener)."""
        return () if self._server is None else tuple(self._server.sockets)

    async def start(self) -> None:
        """Bind the socket and start serving. Raises ``LinkServerError``."""
        if self._server is not None:
            raise LinkServerError("already_started")
        sock = _bind(self._config.socket_path)
        self._server = await unix_serve(
            self._handle,
            sock=sock,
            max_size=_MAX_MESSAGE_BYTES,
            compression=None,  # local link: nothing to gain, and no inflated sizes to police
            open_timeout=HELLO_TIMEOUT_S,
            logger=_library_log,
        )
        log.event("link_listening")

    async def stop(self) -> None:
        """Close the connection, stop any running work and remove the socket."""
        if self._server is None:
            return
        server, self._server = self._server, None
        server.close()  # asyncio removes the socket file it was given (checked by inode)
        await server.wait_closed()
        if self._prewarm is not None:
            self._prewarm.cancel()
            await asyncio.gather(self._prewarm, return_exceptions=True)
            self._prewarm = None
        log.event("link_stopped")

    # ---- connections ---------------------------------------------------------------------

    async def _handle(self, ws: ServerConnection) -> None:
        try:
            if self._handshakes >= MAX_HANDSHAKES:
                await ws.close(1013)
                return
            self._handshakes += 1
            try:
                hello = await self._handshake(ws)
            finally:
                self._handshakes -= 1
            if hello is None:
                return
            if self._active is not None:
                await self._refuse(ws, "busy", hello.id)
                return
            connection = _Connection(self, ws)
            self._active = connection
            log.event("link_connected")
            try:
                await connection.run(hello.id)
            finally:
                await connection.shutdown()
                self._active = None
                log.event("link_disconnected")
        except ConnectionClosed:
            pass
        except Exception as exc:
            log.warning("link_handler_failed", error=type(exc).__name__)

    async def _handshake(self, ws: ServerConnection) -> Hello | None:
        """The peer's valid, correctly tokened hello, or ``None`` after refusing it."""
        try:
            async with asyncio.timeout(HELLO_TIMEOUT_S):
                first = await ws.recv()
        except TimeoutError:
            await self._refuse(ws, "bad_hello")
            return None
        try:
            message = decode_host(first) if isinstance(first, str) else None
        except LinkError:
            await self._refuse(ws, "bad_version" if _other_version(first) else "bad_hello")
            return None
        if not isinstance(message, Hello):
            await self._refuse(ws, "bad_hello")
            return None
        if not hmac.compare_digest(message.token.encode(), self._token):
            await self._refuse(ws, "bad_token", message.id)
            return None
        return message

    async def _refuse(self, ws: ServerConnection, reason: str, request_id: str = _NO_ID) -> None:
        log.warning("link_refused", reason=reason)
        await ws.send(encode(Refused.model_validate({"id": request_id, "reason": reason})))
        await ws.close(1008)

    # ---- shared model work ---------------------------------------------------------------

    async def model_status(self) -> tuple[ModelStatus, ...]:
        """What the answerer says about its models; empty if it cannot say."""
        try:
            return tuple(await self._answerer.status())[:8]
        except Exception as exc:
            log.warning("status_failed", error=type(exc).__name__)
            return ()

    def prewarm(self, role: Role) -> None:
        """Load a model in the background. One at a time; extra requests are skipped."""
        if self._prewarm is not None and not self._prewarm.done():
            log.event("prewarm_skipped", role=role)
            return
        self._prewarm = asyncio.create_task(self._run_prewarm(role))

    async def _run_prewarm(self, role: Role) -> None:
        started = self._clock()
        try:
            await self._answerer.prewarm(role)
        except ProviderError as exc:
            log.warning("prewarm_failed", role=role, code=exc.code)
        except Exception as exc:
            log.warning("prewarm_failed", role=role, error=type(exc).__name__)
        else:
            log.event("prewarm_done", role=role, ms=_ms(self._clock() - started))


def _other_version(first: str | bytes) -> bool:
    """True if a rejected first message is a hello from another protocol version."""
    if not isinstance(first, str):
        return False
    try:
        raw = json.loads(first)
    except ValueError:
        return False
    return isinstance(raw, dict) and raw.get("type") == "hello" and raw.get("v") != 1


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


class _Connection:
    """The one authenticated host connection: its images and its (single) running ask."""

    def __init__(self, server: LinkServer, ws: ServerConnection):
        self._server = server
        self._ws = ws
        self._images = ImageStore(server._clock)
        self._current: OpenRequest | None = None  # the ask that has no terminal message yet
        self._answer_task: asyncio.Task[None] | None = None
        self._late_status: asyncio.Task[None] | None = None

    async def run(self, hello_id: str) -> None:
        await self._welcome(hello_id)
        try:
            async for message in self._ws:
                await self._on_message(message)
        except LinkError as exc:
            log.warning("link_violation", code=exc.code)
            await self._ws.close(1009 if exc.code == "too_large" else 1008)

    async def shutdown(self) -> None:
        """The host is gone: stop the answer and forget its images."""
        tasks = [task for task in (self._answer_task, self._late_status) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._images.clear()

    async def _send(self, message: Outgoing) -> None:
        with contextlib.suppress(ConnectionClosed):
            await self._ws.send(encode(message))

    async def _welcome(self, hello_id: str) -> None:
        status = asyncio.create_task(self._server.model_status())
        done, _ = await asyncio.wait({status}, timeout=STATUS_TIMEOUT_S)
        models = status.result() if done else ()
        await self._send(Welcome(id=hello_id, server_version=self._server._version, models=models))
        if not done:  # the model server is slow: follow up when it answers
            self._late_status = asyncio.create_task(self._send_state(hello_id, status))

    async def _send_state(
        self, hello_id: str, status: asyncio.Task[tuple[ModelStatus, ...]]
    ) -> None:
        try:
            models = await status
        except asyncio.CancelledError:
            status.cancel()
            raise
        await self._send(State(id=hello_id, models=models))

    # ---- incoming messages ---------------------------------------------------------------

    async def _on_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            image_id, _mime, data = decode_image(raw)
            self._images.put(image_id, data)
            asyncio.get_running_loop().call_later(IMAGE_TTL_SECONDS + 1, self._images.expire)
            log.event("image_received", size=len(data), pending=len(self._images))
            return
        message = decode_host(raw)
        match message:
            case Hello():
                raise LinkError("unexpected_hello")
            case Ping():
                await self._send(Pong(id=message.id))
            case Prewarm():
                self._server.prewarm(message.role)
            case Cancel():
                self._on_cancel(message)
            case ToolResult():
                request = self._current
                if request is None or request.id != message.id or not request.resolve(message):
                    log.warning("tool_result_ignored")
            case Ask():
                await self._on_ask(message)

    def _on_cancel(self, message: Cancel) -> None:
        request = self._current
        if request is None or request.id != message.id or request.task is None:
            log.event("cancel_ignored")
            return
        request.cancel_requested = True
        if request.started:
            request.task.cancel()
        # else: cancelling a task that has not run yet would skip ``_answer`` altogether and
        # leave the ask without its terminal message; ``_answer`` checks the flag itself.

    async def _on_ask(self, ask: Ask) -> None:
        image_ids = [ref.image_id for ref in ask.images]
        kinds = [ref.kind for ref in ask.images]
        if self._current is not None:
            if ask.id == self._current.id:
                raise LinkError("duplicate_id")
            self._images.claim(image_ids)  # this ask will not run: do not keep its images
            log.event("ask_refused", code="busy")
            await self._send(AnswerError(id=ask.id, code="busy"))
            return
        data = self._images.claim(image_ids)
        if data is None or len(set(kinds)) != len(kinds):
            log.warning("ask_refused", code="bad_request")
            await self._send(AnswerError(id=ask.id, code="bad_request"))
            return
        limits = self._server._config.limits
        request = OpenRequest(
            ask.id, self._send, max_chars=limits.max_answer_chars, clock=self._server._clock
        )
        context = AskContext(
            request_id=ask.id,
            text=ask.text,
            app=ask.app,
            window=ask.window,
            images=dict(zip(kinds, data, strict=True)),
            can_see=ask.can_see,
            blind_reason=ask.blind_reason,
        )
        self._current = request
        log.event("ask_started", images=len(data), can_see=ask.can_see)
        request.task = self._answer_task = asyncio.create_task(self._answer(request, context))

    # ---- answering -----------------------------------------------------------------------

    async def _answer(self, request: OpenRequest, context: AskContext) -> None:
        """Run the answerer and end the request with exactly one done or error message."""
        clock = self._server._clock
        started = clock()
        deadline = asyncio.timeout(self._server._config.limits.answer_timeout_s)
        final: AnswerDone | AnswerError
        request.started = True
        try:
            if request.cancel_requested:  # cancelled before this task ran: never ask the model
                raise asyncio.CancelledError
            async with deadline:
                result = await self._server._answerer.answer(context, request, request)
            if request.full:
                # The cap's cancel request has not landed yet; withdraw it.
                asyncio.current_task().uncancel()  # type: ignore[union-attr]
            final = AnswerDone(
                id=request.id, sources=result.sources, role=result.role, timings=result.timings
            )
        except asyncio.CancelledError:
            if not (request.full or request.cancel_requested):
                request.close()  # the connection is going away: nobody to tell
                raise
            first = request.first_delta_at
            timings = Timings(
                first_delta_ms=None if first is None else _ms(first - started),
                total_ms=_ms(clock() - started),
            )
            final = AnswerDone(id=request.id, timings=timings)
        except TimeoutError:
            final = _error(request.id, "model_timeout" if deadline.expired() else "internal")
        except ProviderError as exc:
            final = _error(request.id, exc.code, waking=exc.waking)
        except Exception as exc:
            log.warning("answer_failed", error=type(exc).__name__)
            final = _error(request.id, "internal")
        if request.cancel_requested:  # the host asked to stop: that is how the ask ends
            final = AnswerError(id=request.id, code="cancelled")
        request.close()
        self._current = None  # a new ask may start as soon as the terminal message is out
        took = _ms(clock() - started)
        if isinstance(final, AnswerDone):
            log.event(
                "ask_done",
                ms=took,
                chars=request.chars,
                tool_calls=request.tool_calls,
                capped=request.full,
            )
        else:
            log.event("ask_failed", code=final.code, waking=final.waking, ms=took)
        await self._send(final)


def _error(request_id: str, code: ErrorCode, *, waking: bool = False) -> AnswerError:
    """An error message; a code outside the protocol's list becomes ``internal``."""
    try:
        return AnswerError(id=request_id, code=code, waking=waking)
    except ValidationError:
        return AnswerError(id=request_id, code="internal")
