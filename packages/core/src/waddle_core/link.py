"""The host <-> brain link protocol (ARCHITECTURE §5.2, D-034).

One WebSocket over a Unix socket in Waddle's private runtime directory. Text messages are
JSON envelopes defined here; binary messages carry images. Both halves import this module,
so they cannot drift.

Conversation shape
------------------
1. Host connects and sends ``hello`` with the token. Brain answers ``welcome`` or ``refused``
   (then closes). Nothing else is accepted before a valid ``hello``.
2. To ask: the host first sends each image as one **binary** message (``encode_image``),
   then ``ask`` naming those images in ``images``. Images not claimed by an ``ask`` within
   ``IMAGE_TTL_SECONDS`` are dropped by the brain.
3. Brain replies, all carrying the ask's ``id``: zero or more ``tool.call`` (host answers
   each with ``tool.result``), zero or more ``answer.delta``, then exactly one
   ``answer.done`` or ``answer.error``.
4. ``cancel`` (same ``id``) stops an answer; the brain still ends it with ``answer.error``
   code ``cancelled``.
5. ``prewarm`` and ``state`` carry no reply obligation. ``ping``/``pong`` are liveness.

Every string from the other side is untrusted. Sizes are capped here and enforced by both.
"""

from __future__ import annotations

import json
import struct
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator
from pydantic_core import PydanticSerializationError

PROTOCOL_VERSION: Final = 1
SOCKET_NAME: Final = "brain.sock"
TOKEN_NAME: Final = "link-token"  # noqa: S105 - a file name, not a secret
TOKEN_BYTES: Final = 32  # stored as 64 hex characters
MAX_CONTROL_BYTES: Final = 256 * 1024
MAX_IMAGE_BYTES: Final = 8 * 1024 * 1024
MAX_IMAGES_PER_ASK: Final = 2
MAX_PENDING_IMAGES: Final = 4
IMAGE_TTL_SECONDS: Final = 30
MAX_QUESTION_CHARS: Final = 4000
MAX_DELTA_CHARS: Final = 4000
MAX_TITLE_CHARS: Final = 200
MAX_TIMING_MS: Final = 24 * 60 * 60 * 1000  # no honest timing is longer than a day

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Role = Literal["fast_vlm", "smart_model", "embedder"]
ErrorCode = Literal[
    "cancelled",
    "busy",
    "model_unreachable",
    "model_missing",
    "model_timeout",
    "bad_model_output",
    "too_large",
    "bad_request",
    "internal",
]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    v: Literal[1] = 1
    id: Id

    @field_validator("v", mode="before")
    @classmethod
    def _version_is_an_int(cls, value: object) -> object:
        # Literal[1] compares by equality, so without this ``true`` and ``1.0`` pass as 1.
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value


# ---- shared payloads ---------------------------------------------------------------------


class AppContext(BaseModel):
    """What the host worked out about the focused app. Schema-validated, short fields only."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    app_id: str = Field("", max_length=128)
    name: str = Field("", max_length=64)
    version: str | None = Field(None, max_length=32)  # normalized "5.3.1" or None if unknown
    version_source: str = Field("unknown", max_length=32)
    packaging: str = Field("unknown", max_length=16)
    channel: str = Field("stable", max_length=16)
    edition: str | None = Field(None, max_length=32)
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class WindowContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    resource_class: str = Field("", max_length=128)
    title: str = Field("", max_length=MAX_TITLE_CHARS)  # untrusted screen text
    width: int = Field(0, ge=0, le=100_000)
    height: int = Field(0, ge=0, le=100_000)
    scale: float = Field(1.0, gt=0, le=8)
    fullscreen: bool = False
    xwayland: bool | None = None


class ImageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    image_id: Id
    kind: Literal["overview", "crop"]


class ModelStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Role
    model: str = Field(max_length=128)
    available: bool  # the weights are present on the model server
    loaded: bool  # in memory right now


class Timings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    first_delta_ms: int | None = Field(None, ge=0, le=MAX_TIMING_MS)
    total_ms: int = Field(0, ge=0, le=MAX_TIMING_MS)
    model_load_ms: int | None = Field(None, ge=0, le=MAX_TIMING_MS)


# ---- host -> brain -----------------------------------------------------------------------


class Hello(_Msg):
    type: Literal["hello"] = "hello"
    token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    client_version: str = Field(max_length=32)


class Ask(_Msg):
    type: Literal["ask"] = "ask"
    text: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)  # typed by the user: trusted
    app: AppContext | None = None
    window: WindowContext | None = None
    images: tuple[ImageRef, ...] = Field((), max_length=MAX_IMAGES_PER_ASK)
    can_see: bool = False
    blind_reason: str | None = Field(None, max_length=32)  # why there is no image, if so


class Cancel(_Msg):
    type: Literal["cancel"] = "cancel"


class Prewarm(_Msg):
    type: Literal["prewarm"] = "prewarm"
    role: Role = "smart_model"


class ToolResult(_Msg):
    type: Literal["tool.result"] = "tool.result"
    call_id: Id
    ok: bool
    data: dict[str, object] | None = None  # untrusted to the brain; size-capped by the host
    error: str | None = Field(None, max_length=64)


class Ping(_Msg):
    type: Literal["ping"] = "ping"


# ---- brain -> host -----------------------------------------------------------------------


class Welcome(_Msg):
    type: Literal["welcome"] = "welcome"
    server_version: str = Field(max_length=32)
    models: tuple[ModelStatus, ...] = Field((), max_length=8)


class Refused(_Msg):
    type: Literal["refused"] = "refused"
    reason: Literal["bad_token", "bad_version", "bad_hello", "busy"]


class AnswerDelta(_Msg):
    type: Literal["answer.delta"] = "answer.delta"
    text: str = Field(min_length=1, max_length=MAX_DELTA_CHARS)


class AnswerDone(_Msg):
    type: Literal["answer.done"] = "answer.done"
    sources: tuple[Literal["screen", "system", "app", "model"], ...] = ()
    role: Role | None = None
    timings: Timings = Field(default_factory=Timings)


class AnswerError(_Msg):
    type: Literal["answer.error"] = "answer.error"
    code: ErrorCode
    waking: bool = False  # true: the model is still loading; try again shortly


class ToolCall(_Msg):
    type: Literal["tool.call"] = "tool.call"
    call_id: Id
    tool: str = Field(max_length=32)  # validated on the host against waddle_core.tools
    args: dict[str, object] = Field(default_factory=dict)


class State(_Msg):
    type: Literal["state"] = "state"
    models: tuple[ModelStatus, ...] = Field((), max_length=8)
    waking: Role | None = None  # a model is being loaded right now


class Pong(_Msg):
    type: Literal["pong"] = "pong"


HostMessage = Annotated[
    Hello | Ask | Cancel | Prewarm | ToolResult | Ping, Field(discriminator="type")
]
BrainMessage = Annotated[
    Welcome | Refused | AnswerDelta | AnswerDone | AnswerError | ToolCall | State | Pong,
    Field(discriminator="type"),
]
_HOST: Final[TypeAdapter[HostMessage]] = TypeAdapter(HostMessage)
_BRAIN: Final[TypeAdapter[BrainMessage]] = TypeAdapter(BrainMessage)


class LinkError(ValueError):
    """A message that breaks the protocol. ``code`` is safe to log; content never is."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def encode(message: _Msg) -> str:
    """The JSON text for one message. Raises ``LinkError`` if it cannot go on the wire."""
    try:
        text = message.model_dump_json(exclude_none=True)
    except PydanticSerializationError:  # e.g. a lone surrogate in tool output
        raise LinkError("bad_message") from None
    if len(text.encode()) > MAX_CONTROL_BYTES:
        raise LinkError("too_large")
    return text


def _decode[T](adapter: TypeAdapter[T], text: str | bytes) -> T:
    try:
        raw = text.encode() if isinstance(text, str) else text
    except UnicodeEncodeError:  # a lone surrogate: not valid text on the wire
        raise LinkError("bad_message") from None
    if len(raw) > MAX_CONTROL_BYTES:
        raise LinkError("too_large")
    try:
        # Strict: the other side is untrusted, so "1" is not 1 and "yes" is not true.
        return adapter.validate_json(raw, strict=True)
    except ValidationError:
        raise LinkError("bad_message") from None


def decode_host(text: str | bytes) -> HostMessage:
    """Parse a message the host sent (used by the brain)."""
    return _decode(_HOST, text)


def decode_brain(text: str | bytes) -> BrainMessage:
    """Parse a message the brain sent (used by the host)."""
    return _decode(_BRAIN, text)


# ---- binary image messages ---------------------------------------------------------------
# 4-byte big-endian header length, a small JSON header, then the encoded image bytes.

_ID: Final[TypeAdapter[str]] = TypeAdapter(Id)
_MIME: Final = {"image/jpeg", "image/png"}
_MAX_HEADER: Final = 512


def encode_image(image_id: str, mime: str, data: bytes) -> bytes:
    if not isinstance(mime, str) or mime not in _MIME:
        raise LinkError("bad_mime")
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise LinkError("too_large")
    try:
        _ID.validate_python(image_id, strict=True)
    except ValidationError:
        raise LinkError("bad_id") from None
    header = json.dumps({"image_id": image_id, "mime": mime}, separators=(",", ":")).encode()
    return struct.pack(">I", len(header)) + header + data


def decode_image(message: bytes) -> tuple[str, str, bytes]:
    """``(image_id, mime, data)`` from a binary message. Raises ``LinkError``."""
    if len(message) > MAX_IMAGE_BYTES + _MAX_HEADER + 4:
        raise LinkError("too_large")
    if len(message) < 5:
        raise LinkError("bad_image")
    (size,) = struct.unpack(">I", message[:4])
    if size > _MAX_HEADER or len(message) <= 4 + size:
        raise LinkError("bad_image")
    try:
        header = json.loads(bytes(message[4 : 4 + size]))
    except (ValueError, RecursionError):
        raise LinkError("bad_image") from None
    if not isinstance(header, dict) or set(header) != {"image_id", "mime"}:
        raise LinkError("bad_image")
    mime = header["mime"]
    if not isinstance(mime, str) or mime not in _MIME:
        raise LinkError("bad_image")
    try:
        image_id = _ID.validate_python(header["image_id"], strict=True)
    except ValidationError:
        raise LinkError("bad_image") from None
    data = bytes(message[4 + size :])
    if len(data) > MAX_IMAGE_BYTES:
        raise LinkError("too_large")
    return image_id, mime, data
