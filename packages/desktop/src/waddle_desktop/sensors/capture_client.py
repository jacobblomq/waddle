"""Client for the ``waddle-capture`` helper (ARCHITECTURE §4.3).

The helper is a separate executable that owns every pixel: it asks KWin for one window,
enforces the privacy gate around the capture, and hands back two *encoded* images or a
reason why there are none. This module only speaks its wire protocol
(``capture-helper/PROTOCOL.md``) over a socket the caller already connected; it does not
start the helper, and it never sees raw pixels.

Everything read from the socket is validated before it is used. A reply that breaks the
protocol, arrives late, or is cut short raises a typed error and closes the client, because a
byte stream that has lost its framing cannot be trusted again. No call can wait longer than
the configured timeout.

Threading: asyncio only; use one client from one event loop (the I/O thread's).
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_FRAME",
    "Blind",
    "BlindReason",
    "CaptureClient",
    "CaptureClientError",
    "CaptureClosedError",
    "CaptureError",
    "CaptureProtocolError",
    "CaptureRequestError",
    "CaptureResult",
    "CaptureTimeoutError",
    "Frame",
    "Hello",
    "Unchanged",
]

MAX_FRAME: Final = 16 * 1024 * 1024
"""Largest frame payload either side may send (PROTOCOL.md §2)."""

MAX_HEADER: Final = 64 * 1024
"""Reply headers are small JSON objects; anything bigger is not a header."""

DEFAULT_TIMEOUT: Final = 10.0
"""Seconds allowed for one request and its complete reply. A capture is bounded inside the
helper at about 4 s per window (2 s for KWin's answer, 2 s for the pixels)."""

_LENGTH: Final = struct.Struct(">I")
_UUID: Final = re.compile(
    r"^\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?$"
)
_HASH: Final = re.compile(r"^[0-9a-f]{16}$")
_CODE: Final = re.compile(r"^[a-z0-9_]{1,64}$")
_BLIND_REASONS: Final = frozenset({"locked", "blocked", "latched", "not_found"})
_REGIMES: Final = frozenset({"restricted", "open", "unknown"})
_MAX_MESSAGE: Final = 300
_MAX_SIDE: Final = 1 << 20

BlindReason = Literal["locked", "blocked", "latched", "not_found"]
Rect = tuple[float, float, float, float]
"""``(x, y, w, h)`` in window-logical coordinates."""


class CaptureClientError(Exception):
    """Base class for everything this client raises on purpose."""


class CaptureProtocolError(CaptureClientError):
    """The helper sent something the protocol does not allow. The client is closed."""


class CaptureTimeoutError(CaptureClientError):
    """The helper did not complete a reply in time. The client is closed."""


class CaptureClosedError(CaptureClientError):
    """The connection is closed: by ``close()``, by the helper, or after an earlier error."""


class CaptureRequestError(CaptureClientError):
    """The helper answered a control request (hello, blind, forget, ping) with an error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Hello:
    version: str
    protocol: int
    kwin: bool
    screenshot2_version: int | None
    permission_regime: Literal["restricted", "open", "unknown"]
    lock_check: bool
    """False: the helper cannot see the lock state; every capture then fails with the
    error ``lock_unknown``."""
    config_ok: bool
    """False: the user's config file exists but could not be read. The helper then refuses
    every capture with the error ``config_unreadable`` (blocked windows still report
    ``blind``), because the file may block windows the helper cannot know about."""
    latched: bool


@dataclass(frozen=True)
class Unchanged:
    """Nothing visible changed since the last frame sent for this window."""

    hash: str


@dataclass(frozen=True)
class Blind:
    """The helper refused to look, or threw away what it saw."""

    reason: BlindReason


@dataclass(frozen=True)
class CaptureError:
    """The capture failed. ``code`` is one of PROTOCOL.md §4; ``message`` is safe to log."""

    code: str
    message: str = ""


@dataclass(frozen=True)
class Frame:
    hash: str
    scale: float
    width: int
    """Native pixel size of the captured window image."""
    height: int
    overview: bytes
    """JPEG of the whole window, downscaled."""
    overview_size: tuple[int, int]
    crop: bytes
    """PNG at native resolution."""
    crop_rect: tuple[int, int, int, int]
    """``(x, y, w, h)`` of the crop in native pixels of the window image."""
    elapsed_ms: int
    popups: int = 0
    """How many of the requested popups were composited."""


CaptureResult = Unchanged | Blind | CaptureError | Frame


# --------------------------------------------------------------------- reply validation
# The helper is our own binary, but its replies still cross a process boundary: every field
# is checked for type and range before it is used, and nothing is trusted to be present.


def _bad(what: str) -> CaptureProtocolError:
    return CaptureProtocolError(f"malformed reply: {what}")


def _str(header: Mapping[str, object], key: str, pattern: re.Pattern[str] | None = None) -> str:
    value = header.get(key)
    if not isinstance(value, str) or len(value) > _MAX_MESSAGE:
        raise _bad(f"{key} is not a short string")
    if pattern is not None and not pattern.fullmatch(value):
        raise _bad(f"{key} has an unexpected form")
    return value


def _bool(header: Mapping[str, object], key: str) -> bool:
    value = header.get(key)
    if not isinstance(value, bool):
        raise _bad(f"{key} is not a boolean")
    return value


def _int(header: Mapping[str, object], key: str, low: int, high: int) -> int:
    value = header.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad(f"{key} is not an integer in range")
    return value


def _object(header: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = header.get(key)
    if not isinstance(value, dict):
        raise _bad(f"{key} is not an object")
    return cast("dict[str, object]", value)


def _frame_sizes(header: Mapping[str, object]) -> list[int]:
    sizes = header.get("frames")
    if not isinstance(sizes, list) or len(sizes) > 2:
        raise _bad("frames is not a short list")
    for size in sizes:
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FRAME:
            raise _bad("frames holds an invalid size")
    return cast("list[int]", sizes)


def _hello(header: Mapping[str, object]) -> Hello:
    version = header.get("screenshot2_version")
    if version is not None:
        version = _int(header, "screenshot2_version", 0, 1_000_000)
    regime = _str(header, "permission_regime")
    if regime not in _REGIMES:
        raise _bad("permission_regime is unknown")
    return Hello(
        version=_str(header, "version"),
        protocol=_int(header, "protocol", 1, 1_000_000),
        kwin=_bool(header, "kwin"),
        screenshot2_version=version,
        permission_regime=cast("Literal['restricted', 'open', 'unknown']", regime),
        lock_check=_bool(header, "lock_check"),
        config_ok=_bool(header, "config_ok"),
        latched=_bool(header, "latched"),
    )


def _frame(header: Mapping[str, object], overview: bytes, crop: bytes) -> Frame:
    scale = header.get("scale")
    if isinstance(scale, bool) or not isinstance(scale, int | float) or not 0 < scale <= 16:
        raise _bad("scale is not a plausible number")
    width = _int(header, "width", 1, _MAX_SIDE)
    height = _int(header, "height", 1, _MAX_SIDE)
    overview_info = _object(header, "overview")
    crop_info = _object(header, "crop")
    if overview_info.get("format") != "jpeg" or crop_info.get("format") != "png":
        raise _bad("unexpected image format")
    crop_rect = (
        _int(crop_info, "x", 0, width - 1),
        _int(crop_info, "y", 0, height - 1),
        _int(crop_info, "w", 1, width),
        _int(crop_info, "h", 1, height),
    )
    if crop_rect[0] + crop_rect[2] > width or crop_rect[1] + crop_rect[3] > height:
        raise _bad("crop lies outside the image")
    if not overview.startswith(b"\xff\xd8") or not crop.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _bad("image data does not match its announced format")
    return Frame(
        hash=_str(header, "hash", _HASH),
        scale=float(scale),
        width=width,
        height=height,
        overview=overview,
        overview_size=(_int(overview_info, "w", 1, width), _int(overview_info, "h", 1, height)),
        crop=crop,
        crop_rect=crop_rect,
        elapsed_ms=_int(header, "elapsed_ms", 0, 3_600_000),
        popups=_int(header, "popups", 0, 64) if "popups" in header else 0,
    )


def _rect(value: Sequence[float], what: str) -> dict[str, float]:
    if len(value) != 4:
        raise ValueError(f"{what} must be (x, y, w, h)")
    x, y, w, h = (float(v) for v in value)
    return {"x": x, "y": y, "w": w, "h": h}


def _uuid(value: str, what: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ValueError(f"{what} is not a window UUID")
    return value


class CaptureClient:
    """Speaks the helper's protocol over an already connected ``SOCK_STREAM`` socket.

    The client takes ownership of ``sock`` and closes it in :meth:`close`. Requests are
    serialized: a second call waits until the first has its complete reply.
    """

    def __init__(self, sock: socket.socket, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._sock: socket.socket | None = sock
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._closed = False
        self._blind_epoch = 0

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ public verbs

    async def hello(self) -> Hello:
        """Ask the helper what it found in this session."""
        header, _ = await self._request({"verb": "hello"})
        self._require_ok(header)
        try:
            return _hello(header)
        except CaptureProtocolError as exc:
            raise self._fail(exc) from None

    async def ping(self) -> None:
        header, _ = await self._request({"verb": "ping"})
        self._require_ok(header)

    async def set_blind(self, on: bool) -> None:
        """Latch (``True``) or release (``False``) the helper's blind state.

        While latched the helper refuses every capture without asking KWin anything, and it
        has dropped everything it remembered about earlier frames.

        Requests are serialized, so the helper only hears this after it has answered any
        capture already under way. From the moment this is called with ``True``, every
        capture that was asked for earlier therefore returns ``Blind("latched")`` and its
        frame is dropped here, unseen: blind means blind (I4).
        """
        if on:
            self._blind_epoch += 1
        header, _ = await self._request({"verb": "blind", "on": bool(on)})
        self._require_ok(header)
        if header.get("latched") is not bool(on):
            raise self._fail(_bad("latch state does not match the request"))

    async def forget(self) -> None:
        """Drop the helper's change-detection memory."""
        header, _ = await self._request({"verb": "forget"})
        self._require_ok(header)

    async def capture_window(
        self,
        uuid: str,
        *,
        popups: Iterable[str] = (),
        roi: Rect | None = None,
        cursor: tuple[float, float] | None = None,
        overview_max: int = 1024,
        crop_size: int = 512,
        live_regions: Iterable[Rect] = (),
        force: bool = False,
    ) -> CaptureResult:
        """Capture one window through the helper's gate.

        ``roi``, ``cursor`` and ``live_regions`` are in window-logical coordinates. ``roi``
        and ``cursor`` are mutually exclusive; they only choose where the native-resolution
        crop is centred. ``live_regions`` are ignored by change detection.

        Returns :class:`Frame`, :class:`Unchanged`, :class:`Blind` or :class:`CaptureError`;
        raises only for bad arguments and for transport or protocol failures.
        """
        if roi is not None and cursor is not None:
            raise ValueError("roi and cursor are mutually exclusive")
        body: dict[str, object] = {
            "verb": "capture_window",
            "uuid": _uuid(uuid, "uuid"),
            "overview_max": int(overview_max),
            "crop_size": int(crop_size),
            "force": bool(force),
        }
        popup_list = [_uuid(p, "popup") for p in popups]
        if popup_list:
            body["popups"] = popup_list
        if roi is not None:
            body["roi"] = _rect(roi, "roi")
        if cursor is not None:
            if len(cursor) != 2:
                raise ValueError("cursor must be (x, y)")
            body["cursor"] = {"x": float(cursor[0]), "y": float(cursor[1])}
        regions = [_rect(r, "live region") for r in live_regions]
        if regions:
            body["live_regions"] = regions

        epoch = self._blind_epoch
        header, frames = await self._request(body)
        try:
            result = self._capture_result(header, frames)
        except CaptureProtocolError as exc:
            raise self._fail(exc) from None
        if epoch != self._blind_epoch and isinstance(result, Frame | Unchanged):
            # Blind was requested while this capture was waiting or in flight.
            return Blind(reason="latched")
        return result

    def close(self) -> None:
        """Close the connection; the helper exits when it sees the socket close."""
        self._closed = True
        writer, self._writer, self._reader = self._writer, None, None
        sock, self._sock = self._sock, None
        if writer is not None:
            writer.close()
        elif sock is not None:
            sock.close()

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _capture_result(header: Mapping[str, object], frames: list[bytes]) -> CaptureResult:
        status = header["status"]
        if status == "ok":
            if len(frames) != 2:
                raise _bad("a captured frame needs exactly two images")
            return _frame(header, frames[0], frames[1])
        if frames:
            raise _bad("images attached to a reply that has none")
        if status == "unchanged":
            return Unchanged(hash=_str(header, "hash", _HASH))
        if status == "blind":
            reason = _str(header, "reason")
            if reason not in _BLIND_REASONS:
                raise _bad("unknown blind reason")
            return Blind(reason=cast("BlindReason", reason))
        return CaptureError(code=_str(header, "code", _CODE), message=_str(header, "message"))

    def _require_ok(self, header: Mapping[str, object]) -> None:
        status = header["status"]
        if status == "ok":
            return
        if status == "error":
            try:
                code, message = _str(header, "code", _CODE), _str(header, "message")
            except CaptureProtocolError as exc:
                raise self._fail(exc) from None
            raise CaptureRequestError(code, message)
        raise self._fail(_bad("unexpected status for a control request"))

    def _fail[E: CaptureClientError](self, error: E) -> E:
        """Close the client and hand the error back for raising."""
        self.close()
        return error

    async def _streams(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            sock = self._sock
            if sock is None:
                raise CaptureClosedError("capture client is closed")
            self._reader, self._writer = await asyncio.open_connection(sock=sock)
        return self._reader, self._writer

    async def _request(self, body: dict[str, object]) -> tuple[dict[str, object], list[bytes]]:
        """Send one request and read its whole reply, under the lock and the timeout."""
        async with self._lock:
            if self._closed:
                raise CaptureClosedError("capture client is closed")
            self._next_id += 1
            request_id = self._next_id
            payload = json.dumps({"id": request_id, **body}, separators=(",", ":")).encode()
            if len(payload) > MAX_FRAME:
                raise ValueError("request is too large")
            try:
                async with asyncio.timeout(self._timeout):
                    return await self._exchange(request_id, payload)
            except TimeoutError:
                raise self._fail(CaptureTimeoutError("capture helper did not answer")) from None
            except asyncio.IncompleteReadError:
                raise self._fail(CaptureClosedError("capture helper closed the socket")) from None
            except OSError:
                raise self._fail(CaptureClosedError("capture socket failed")) from None
            except CaptureProtocolError as exc:
                raise self._fail(exc) from None
            except asyncio.CancelledError:
                # A reply may be half read: the stream cannot be reused.
                self.close()
                raise

    async def _exchange(
        self, request_id: int, payload: bytes
    ) -> tuple[dict[str, object], list[bytes]]:
        reader, writer = await self._streams()
        writer.write(_LENGTH.pack(len(payload)) + payload)
        await writer.drain()

        raw_header = await self._read_frame(reader, MAX_HEADER)
        try:
            parsed = json.loads(raw_header)
        except (ValueError, RecursionError):
            raise _bad("header is not JSON") from None
        if not isinstance(parsed, dict):
            raise _bad("header is not an object")
        header = cast("dict[str, object]", parsed)
        reply_id = header.get("id")
        if type(reply_id) is not int or reply_id != request_id:
            raise _bad("reply id does not match the request")
        if header.get("status") not in ("ok", "unchanged", "blind", "error"):
            raise _bad("unknown status")
        sizes = _frame_sizes(header)
        if sizes and header["status"] != "ok":
            raise _bad("binary frames announced for a reply that has none")
        frames = [await self._read_frame(reader, size, exact=True) for size in sizes]
        return header, frames

    @staticmethod
    async def _read_frame(
        reader: asyncio.StreamReader, limit: int, *, exact: bool = False
    ) -> bytes:
        (length,) = _LENGTH.unpack(await reader.readexactly(_LENGTH.size))
        if length > MAX_FRAME:
            raise _bad("frame exceeds the maximum size")
        if length > limit or (exact and length != limit):
            raise _bad("frame size differs from what was expected")
        return await reader.readexactly(length)
