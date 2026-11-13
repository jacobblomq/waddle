"""The host's connection to waddle-brain (ARCHITECTURE §5.2, §8 "Brain unreachable").

``BrainLink`` lives on the I/O thread's asyncio loop: every method is called on that loop and
every callback is invoked on it (forward to the UI with queued signals). It keeps trying to
reach the brain, tells its owner whether the brain is up, and carries questions and answers.

Rules this module keeps:

- The token is sent only to a socket that is ours: a socket we own, in a directory we own
  that nobody else can enter. Symlinks are never followed.
- Everything the brain sends is untrusted: it is schema-checked, and a tool call is run only
  for a request this host opened, under the per-request cap.
- Each request ends with exactly one ``on_done`` or ``on_error``, whatever the brain does.
- Images are bytes in memory on their way to the socket. Logs carry event names, codes and
  counts only: never text, titles, ids from the brain or the token.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import secrets
import stat
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from waddle_core.link import (
    MAX_CONTROL_BYTES,
    AnswerDelta,
    AnswerDone,
    AnswerError,
    AppContext,
    Ask,
    Cancel,
    Hello,
    ImageRef,
    LinkError,
    ModelStatus,
    Pong,
    Prewarm,
    Refused,
    Role,
    State,
    ToolCall,
    ToolResult,
    Welcome,
    WindowContext,
    decode_brain,
    encode,
    encode_image,
)
from waddle_core.tools import MAX_TOOL_CALLS_PER_REQUEST
from waddle_desktop import log
from waddle_desktop.link.token import read_token
from waddle_desktop.link.tools_host import HostToolDispatcher

BACKOFF_FIRST: Final = 0.5
BACKOFF_CAP: Final = 30.0
STABLE_AFTER: Final = 30.0  # a link that stayed up this long restarts the backoff schedule
HANDSHAKE_TIMEOUT: Final = 5.0
CLOSE_TIMEOUT: Final = 2.0  # how long a closing handshake may hold us up
MAX_STRAYS: Final = 32  # brain messages for no open request tolerated per question
KEEPALIVE: Final = 20.0  # ping every 20 s; no pong within 20 s closes the connection
BRAIN_LOST: Final = "brain_lost"  # host-side error code: the brain went away or misbehaved
CANCELLED: Final = "cancelled"
_REMEMBER_ENDED: Final = 64
_VIOLATION_CLOSES: Final = frozenset({1002, 1003, 1007, 1009})  # close codes we send

ImageKind = Literal["overview", "crop"]
Image = tuple[ImageKind, str, bytes]  # kind, mime, encoded bytes


class BrainDownError(Exception):
    """Raised by ``ask`` when there is no working link to the brain."""


@dataclass(frozen=True)
class LinkState:
    """What the UI needs to know about the brain. ``reason`` is a short code, set when down."""

    status: Literal["down", "connecting", "up"]
    reason: str = ""
    models: tuple[ModelStatus, ...] = ()
    waking: Role | None = None


class LinkSocket(Protocol):
    """The part of a websockets connection the link uses."""

    async def send(self, message: str | bytes) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self, code: int = 1000) -> None: ...


Connector = Callable[[Path], Awaitable[LinkSocket]]

# The websockets library logs whole frames at DEBUG (question, window title, answer text).
# It gets a logger that goes nowhere, whatever the root logger's level is.
_library_log = logging.getLogger("waddle.websockets")
_library_log.addHandler(logging.NullHandler())
_library_log.propagate = False
_library_log.setLevel(logging.CRITICAL + 1)


async def _unix_connector(path: Path) -> LinkSocket:
    return await unix_connect(
        str(path),
        max_size=MAX_CONTROL_BYTES,  # the brain sends control messages only, never images
        open_timeout=HANDSHAKE_TIMEOUT,
        close_timeout=CLOSE_TIMEOUT,
        ping_interval=KEEPALIVE,
        ping_timeout=KEEPALIVE,
        proxy=None,
        logger=_library_log,
    )


def check_socket(path: Path) -> str | None:
    """Why ``path`` must not be given the token, or ``None`` if it is safe.

    Safe means: its directory is a real directory of ours with mode 0700, and the path itself
    is a socket we own. Nobody else can then swap it between this check and the connect.
    """
    try:
        parent = os.lstat(path.parent)
        info = os.lstat(path)
    except FileNotFoundError:
        return "no_socket"
    except OSError:
        return "unsafe_socket"
    # UNVERIFIED (spike S3.4): this needs the containerised brain to create the socket under
    # the host user's uid (user namespace mapping). Not tested: no container runtime here.
    uid = os.getuid()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid:
        return "unsafe_socket"
    if stat.S_IMODE(parent.st_mode) != 0o700:
        return "unsafe_socket"
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid:
        return "unsafe_socket"
    return None


@dataclass
class _Request:
    on_delta: Callable[[str], None]
    on_done: Callable[[AnswerDone], None]
    on_error: Callable[[str], None]
    tool_calls: int = 0


class BrainLink:
    """Connects to the brain, reconnects when it goes away, and routes requests."""

    def __init__(
        self,
        socket_path: Path,
        token_path: Path,
        *,
        client_version: str,
        tools: HostToolDispatcher,
        on_state: Callable[[LinkState], None],
        connector: Connector | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self._socket_path = socket_path
        self._token_path = token_path
        self._client_version = client_version
        self._tools = tools
        self._on_state = on_state
        self._connect = connector or _unix_connector
        self._sleep = sleep
        self._jitter = jitter
        self._state = LinkState("down", "stopped")
        self._task: asyncio.Task[None] | None = None
        self._kick = asyncio.Event()
        self._outbox: asyncio.Queue[str | bytes] | None = None  # set while up
        self._requests: dict[str, _Request] = {}
        self._ended: deque[str] = deque(maxlen=_REMEMBER_ENDED)  # cancelled here, brain may lag
        self._tool_tasks: set[asyncio.Task[None]] = set()
        self._strays = 0  # since the last ask; see _stray

    @property
    def state(self) -> LinkState:
        return self._state

    # ---- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        """Begin connecting. Call on the I/O loop."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    def kick(self) -> None:
        """Retry now instead of waiting out the backoff (the user just asked something)."""
        self._kick.set()

    async def stop(self) -> None:
        """Close the link. Open requests end with ``brain_lost``."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._set_state(LinkState("down", "stopped"))

    # ---- requests ------------------------------------------------------------------------

    def ask(
        self,
        text: str,
        *,
        app: AppContext | None,
        window: WindowContext | None,
        images: Sequence[Image] = (),
        can_see: bool,
        blind_reason: str | None,
        on_delta: Callable[[str], None],
        on_done: Callable[[AnswerDone], None],
        on_error: Callable[[str], None],
    ) -> str:
        """Send a question; returns the request id.

        Raises ``BrainDownError`` if the link is not up, and ``ValueError`` if the question or
        an image cannot go on the wire (too long, too large, wrong type). In both cases
        nothing was sent and no callback will be called.
        """
        outbox = self._outbox
        if outbox is None or self._state.status != "up":
            raise BrainDownError
        request_id = _new_id()
        refs: list[ImageRef] = []
        frames: list[bytes] = []
        for kind, mime, data in images:
            image_id = _new_id()
            frames.append(encode_image(image_id, mime, data))
            refs.append(ImageRef(image_id=image_id, kind=kind))
        message = encode(
            Ask(
                id=request_id,
                text=text,
                app=app,
                window=window,
                images=tuple(refs),
                can_see=can_see,
                blind_reason=blind_reason,
            )
        )
        self._requests[request_id] = _Request(on_delta, on_done, on_error)
        self._strays = 0
        for frame in frames:  # images first: the ask names images the brain already holds
            outbox.put_nowait(frame)
        outbox.put_nowait(message)
        log.event("link_ask", images=len(frames), bytes=sum(map(len, frames)), can_see=can_see)
        return request_id

    def cancel(self, request_id: str) -> None:
        """Stop a request. It ends at once with ``on_error("cancelled")``; the brain is told."""
        request = self._requests.pop(request_id, None)
        if request is None:
            return
        self._ended.append(request_id)
        if self._outbox is not None:
            self._outbox.put_nowait(encode(Cancel(id=request_id)))
        _call(request.on_error, CANCELLED)

    def prewarm(self, role: Role) -> None:
        """Ask the brain to load a model ahead of a question. Does nothing while down."""
        if self._outbox is not None:
            self._outbox.put_nowait(encode(Prewarm(id=_new_id(), role=role)))

    # ---- connect loop --------------------------------------------------------------------

    async def _run(self) -> None:
        failures = 0
        while True:
            outcome: Literal["failed", "slow", "lost"]
            try:
                outcome = await self._attempt()
            except Exception as exc:  # a bug here must not end the link for good
                outcome = self._down("internal", "failed")
                log.warning("link_internal_error", kind=type(exc).__name__)
            if outcome == "lost":  # it was up and stayed up: start the schedule again
                failures = 0
            delay = BACKOFF_CAP if outcome == "slow" else self._backoff(failures)
            failures += 1
            await self._wait(delay)

    @staticmethod
    def _backoff(failures: int) -> float:
        return float(min(BACKOFF_CAP, BACKOFF_FIRST * 2 ** min(failures, 16)))

    async def _wait(self, delay: float) -> None:
        """Sleep ``delay`` less up to a quarter of jitter, or until ``kick``."""
        self._kick.clear()
        sleeper = asyncio.ensure_future(self._sleep(delay * (1.0 - 0.25 * self._jitter())))
        kicked = asyncio.ensure_future(self._kick.wait())
        try:
            await asyncio.wait({sleeper, kicked}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            kicked.cancel()

    async def _attempt(self) -> Literal["failed", "slow", "lost"]:
        """One connection, from checks to its end. Says how soon to try again."""
        unsafe = check_socket(self._socket_path)
        if unsafe is not None:
            return self._down(unsafe, "failed")
        token = read_token(self._token_path)
        if token is None:
            return self._down("no_token", "failed")
        self._set_state(LinkState("connecting"))
        try:
            ws = await self._connect(self._socket_path)
        except (OSError, WebSocketException, TimeoutError):
            return self._down("connect_failed", "failed")
        close_code = 1000
        try:
            welcome = await self._handshake(ws, token)
            if isinstance(welcome, str):
                # A wrong token will not fix itself: do not hammer the brain with it.
                return self._down(welcome, "slow" if welcome == "bad_token" else "failed")
            self._outbox = asyncio.Queue()
            self._strays = 0
            self._set_state(LinkState("up", models=welcome.models))
            clock = asyncio.get_running_loop().time
            up_since = clock()
            reason = await self._session(ws, self._outbox)
            if reason == "protocol":
                # A brain that welcomes us and then breaks the protocol is not worth a fast
                # retry.
                close_code = 1008
                return self._down(reason, "slow")
            # A brain that welcomes us and goes away at once (a crash loop) keeps backing off.
            stable = clock() - up_since >= STABLE_AFTER
            return self._down(reason, "lost" if stable else "failed")
        finally:
            # Requests end first: a brain that will not finish closing must not hold them up.
            self._outbox = None
            self._fail_all()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await asyncio.shield(ws.close(close_code))

    def _down[T](self, reason: str, outcome: T) -> T:
        self._set_state(LinkState("down", reason))
        return outcome

    async def _handshake(self, ws: LinkSocket, token: str) -> Welcome | str:
        """Send the token and wait for the verdict: the welcome, or a reason code."""
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                hello = Hello(id=_new_id(), token=token, client_version=self._client_version)
                await ws.send(encode(hello))
                raw = await ws.recv()
        except TimeoutError:
            return "handshake_timeout"
        except (ConnectionClosed, OSError):
            return "connect_failed"
        if not isinstance(raw, str):  # control messages are text; binary is for images
            return "bad_handshake"
        try:
            message = decode_brain(raw)
        except LinkError:
            return "bad_handshake"
        if isinstance(message, Welcome):
            return message
        if isinstance(message, Refused):
            return message.reason
        return "bad_handshake"

    # ---- one connection ------------------------------------------------------------------

    async def _session(self, ws: LinkSocket, outbox: asyncio.Queue[str | bytes]) -> str:
        """Pump messages until the connection ends; returns why (``lost`` or ``protocol``)."""
        writer = asyncio.ensure_future(self._write(ws, outbox))
        try:
            while True:
                raw = await ws.recv()
                if not isinstance(raw, str):
                    raise LinkError("unexpected_binary")
                await self._incoming(decode_brain(raw), outbox)
        except ConnectionClosed as exc:
            if exc.sent is not None and exc.sent.code in _VIOLATION_CLOSES:
                # The socket layer refused what the brain sent (too big, not text, malformed).
                log.warning("link_protocol_violation", code="bad_frame")
                return "protocol"
            return "lost"
        except OSError:
            return "lost"
        except LinkError as exc:
            log.warning("link_protocol_violation", code=exc.code)
            return "protocol"
        finally:
            writer.cancel()
            for task in self._tool_tasks:
                task.cancel()

    @staticmethod
    async def _write(ws: LinkSocket, outbox: asyncio.Queue[str | bytes]) -> None:
        with contextlib.suppress(ConnectionClosed, OSError):  # the reader reports the loss
            while True:
                await ws.send(await outbox.get())

    async def _incoming(self, message: object, outbox: asyncio.Queue[str | bytes]) -> None:
        match message:
            case AnswerDelta():
                if request := self._open(message.id, "answer.delta"):
                    _call(request.on_delta, message.text)
            case AnswerDone():
                if request := self._open(message.id, "answer.done", end=True):
                    _call(request.on_done, message)
            case AnswerError():
                if request := self._open(message.id, "answer.error", end=True):
                    log.event("link_answer_error", code=message.code, waking=message.waking)
                    # A timeout while the model is still loading is reported as its own code.
                    waking = message.waking and message.code == "model_timeout"
                    _call(request.on_error, "waking" if waking else message.code)
            case ToolCall():
                await self._tool_call(message, outbox)
            case State():
                self._set_state(LinkState("up", models=message.models, waking=message.waking))
            case Pong():
                pass
            case _:  # a second welcome, or refused after welcome
                raise LinkError("unexpected_message")

    def _open(self, request_id: str, kind: str, *, end: bool = False) -> _Request | None:
        """The open request a brain message belongs to; unknown ids are dropped and logged."""
        request = self._requests.pop(request_id, None) if end else self._requests.get(request_id)
        if request is None and request_id not in self._ended:
            self._stray()
            log.warning("link_unknown_id", type=kind)
        return request

    def _stray(self) -> None:
        """Count a brain message that belongs to no open request; too many end the link.

        Each one costs a log line and perhaps a reply, so a brain must not be able to send
        them without limit. An honest brain sends next to none.
        """
        self._strays += 1
        if self._strays > MAX_STRAYS:
            raise LinkError("too_many_strays")

    async def _tool_call(self, call: ToolCall, outbox: asyncio.Queue[str | bytes]) -> None:
        request = self._requests.get(call.id)
        calls_so_far = request.tool_calls if request else 0
        if request is None or calls_so_far >= MAX_TOOL_CALLS_PER_REQUEST:
            # A refusal needs no backend: answer in line, so a flood cannot pile up tasks.
            self._stray()
            await self._answer_tool(request is not None, calls_so_far, call, outbox)
            return
        request.tool_calls += 1
        task = asyncio.ensure_future(self._answer_tool(True, calls_so_far, call, outbox))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _answer_tool(
        self, is_open: bool, calls_so_far: int, call: ToolCall, outbox: asyncio.Queue[str | bytes]
    ) -> None:
        result = await self._tools.handle(is_open, calls_so_far, call)
        try:
            text = encode(result)
        except LinkError as exc:  # e.g. text that is not valid on the wire
            log.warning("tool_result_unsendable", code=exc.code)
            text = encode(
                ToolResult(id=call.id, call_id=call.call_id, ok=False, error="bad_result")
            )
        outbox.put_nowait(text)

    # ---- helpers -------------------------------------------------------------------------

    def _fail_all(self) -> None:
        requests, self._requests = self._requests, {}
        for request in requests.values():
            _call(request.on_error, BRAIN_LOST)

    def _set_state(self, state: LinkState) -> None:
        if state != self._state:
            self._state = state
            log.event("link_state", status=state.status, reason=state.reason or "none")
            try:
                self._on_state(state)
            except Exception:  # a broken listener must not break the link
                log.warning("link_callback_failed", callback="on_state")


def _new_id() -> str:
    return secrets.token_urlsafe(12)


def _call[T](callback: Callable[[T], None], value: T) -> None:
    try:
        callback(value)
    except Exception:  # a broken listener must not break the link
        log.warning("link_callback_failed", callback="request")
