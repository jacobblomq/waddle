"""Ollama as a ``ModelProvider`` (ARCHITECTURE §5.3).

Talks to exactly one address, ``config.ollama_url``, over plain HTTP on the internal
container network. Redirects are never followed and proxy environment variables are
ignored, so nothing is ever sent anywhere else.

API facts used here, checked against Ollama's ``docs/api.md`` and ``docs/api/errors.mdx``:

- ``POST /api/chat`` streams NDJSON objects ``{"message": {"content": ...}, "done": bool}``.
  ``format`` takes a JSON schema, ``images`` on a message is a list of base64 strings,
  ``options.num_ctx`` sets the context size, ``keep_alive`` takes seconds as a number.
- Thinking models put reasoning in ``message.thinking``; it is not part of the reply and is
  dropped here.
- An empty ``messages`` list loads the model; with ``keep_alive: 0`` it unloads it.
- Errors are ``{"error": "..."}``: as the body of a non-200 reply (404 for a missing
  model), or as a line in the middle of a stream that already started with 200.
- ``GET /api/tags`` lists pulled models, ``GET /api/ps`` lists models in memory.

Everything the server sends is untrusted: lines and bodies are size-capped, parsed
defensively, and never logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Final

import httpx
from pydantic import BaseModel

from waddle_brain import log
from waddle_brain.config import BrainConfig, RoleConfig
from waddle_brain.contracts import ChatMessage, ProviderError
from waddle_core.link import ErrorCode, ModelStatus, Role

MAX_LINE_BYTES: Final = 256 * 1024  # one NDJSON line; real ones are a few hundred bytes
MAX_STREAM_BYTES: Final = 4 * 1024 * 1024  # one whole reply, thinking included
MAX_BODY_BYTES: Final = 4 * 1024 * 1024  # a non-streamed reply (model lists, errors)
_CONNECT_TIMEOUT_S: Final = 5.0
_QUICK_TIMEOUT_S: Final = 5.0  # for the small status calls
# Compressed replies are not asked for and not accepted: the HTTP library would inflate
# them before the size caps here get to count anything.
_HEADERS: Final = {"Accept-Encoding": "identity"}

type _Json = dict[str, object]


class OllamaProvider:
    """One Ollama server. Safe to share between tasks; each call is its own request."""

    def __init__(self, config: BrainConfig, *, client: httpx.AsyncClient | None = None):
        self._config = config
        self._base = config.ollama_url
        self._owns_client = client is None
        # trust_env=False: a proxy variable in the environment must not reroute requests.
        self._client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)

    # ---- chat ----------------------------------------------------------------------------

    async def chat(
        self,
        role: Role,
        messages: Sequence[ChatMessage],
        *,
        schema: type[BaseModel] | None = None,
    ) -> AsyncIterator[str]:
        """Stream the reply as text pieces. Closing or cancelling the generator closes the
        HTTP stream, which is what makes the server stop generating. Callers that may stop
        early should wrap the generator in ``contextlib.aclosing``."""
        cfg = self._role(role)
        body = self._chat_body(role, cfg, messages, schema)
        limits = self._config.limits
        timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT_S,
            read=limits.first_token_timeout_s,  # also the longest allowed gap mid-stream
            write=_CONNECT_TIMEOUT_S,
            pool=_CONNECT_TIMEOUT_S,
        )
        started = time.monotonic()
        chars = 0
        try:
            async with self._client.stream(
                "POST",
                f"{self._base}/api/chat",
                json=body,
                headers=_HEADERS,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                _refuse_encoded(response)
                if response.status_code != httpx.codes.OK:
                    raise _status_error(response.status_code, await _read_capped(response))
                done = False
                async for line in _lines(response):
                    piece, done = _parse_chat_line(line)
                    if piece:
                        chars += len(piece)
                        yield piece
                    if done:
                        break
                if not done:
                    raise ProviderError("bad_model_output")  # the stream was cut short
        except ProviderError as exc:
            log.warning("model_chat_failed", role=role, code=exc.code, waking=exc.waking)
            raise
        except httpx.HTTPError as exc:
            error = _transport_error(exc)
            if error.code == "model_timeout" and chars == 0:
                error = ProviderError("model_timeout", waking=await self._still_loading(cfg))
            log.warning("model_chat_failed", role=role, code=error.code, waking=error.waking)
            raise error from None
        log.event("model_chat_done", role=role, chars=chars, ms=_ms_since(started))

    def _chat_body(
        self,
        role: Role,
        cfg: RoleConfig,
        messages: Sequence[ChatMessage],
        schema: type[BaseModel] | None,
    ) -> _Json:
        if role == "embedder":
            raise ProviderError("bad_request")  # an embedding model cannot chat
        wire: list[_Json] = []
        for message in messages:
            try:
                message.content.encode("utf-8")
            except UnicodeEncodeError:  # a lone surrogate, e.g. from a JSON escape
                raise ProviderError("bad_request") from None
            item: _Json = {"role": message.role, "content": message.content}
            if message.images:
                if not cfg.vision or message.role != "user":
                    raise ProviderError("bad_request")
                item["images"] = [
                    base64.b64encode(image).decode("ascii") for image in message.images
                ]
            wire.append(item)
        body: _Json = {
            "model": cfg.model,
            "messages": wire,
            "stream": True,
            "keep_alive": cfg.keep_alive_s,
            "options": {"num_ctx": cfg.num_ctx},
        }
        if schema is not None:
            body["format"] = schema.model_json_schema()
        return body

    async def _still_loading(self, cfg: RoleConfig) -> bool:
        """After a timeout with no reply text: is the model simply not in memory yet?

        UNVERIFIED (spike S3.2): assumes /api/ps lists a model only once it has finished
        loading. If Ollama lists it while it loads, a slow cold start is reported as a
        plain timeout instead of "waking up"; nothing worse.
        """
        try:
            loaded = await self._names("/api/ps")
        except ProviderError:
            return False
        return _canonical(cfg.model) not in loaded

    # ---- load / unload -------------------------------------------------------------------

    async def load(self, role: Role) -> None:
        """Bring the role's model into memory with its configured context size."""
        cfg = self._role(role)
        body: _Json = {
            "model": cfg.model,
            "keep_alive": cfg.keep_alive_s,
            "options": {"num_ctx": cfg.num_ctx},
        }
        if role == "embedder":
            # UNVERIFIED (spike S3.2): an embedding model is refused by /api/chat, so it is
            # loaded by embedding one character.
            path = "/api/embed"
            body["input"] = "a"
        else:
            path = "/api/chat"
            body["messages"] = []
            body["stream"] = False
        started = time.monotonic()
        await self._post(role, "load", path, body, self._config.limits.load_timeout_s)
        log.event("model_loaded", role=role, ms=_ms_since(started))

    async def unload(self, role: Role) -> None:
        """Ask the server to drop the role's model from memory now.

        UNVERIFIED (spike S3.2): for the embedder this relies on /api/chat handling the
        unload request before it checks that the model can chat.
        """
        cfg = self._role(role)
        body: _Json = {"model": cfg.model, "messages": [], "stream": False, "keep_alive": 0}
        await self._post(role, "unload", "/api/chat", body, _QUICK_TIMEOUT_S * 4)
        log.event("model_unloaded", role=role)

    async def _post(self, role: Role, what: str, path: str, body: _Json, read_s: float) -> None:
        timeout = httpx.Timeout(_CONNECT_TIMEOUT_S, read=read_s)
        try:
            data = await self._request("POST", path, timeout, body)
            if isinstance(data.get("error"), str):
                raise _message_error(str(data["error"]))
        except ProviderError as exc:
            log.warning("model_call_failed", what=what, role=role, code=exc.code)
            raise

    # ---- status --------------------------------------------------------------------------

    async def status(self) -> list[ModelStatus]:
        """One entry per configured role: are the weights pulled, and are they in memory.

        ``loaded`` means listed by /api/ps, even if part of the model sits on the CPU
        (``size_vram`` < ``size``); measuring that is the VRAM governor's business.
        """
        available = await self._names("/api/tags")
        loaded = await self._names("/api/ps")
        return [
            ModelStatus(
                role=role,
                model=cfg.model,
                available=_canonical(cfg.model) in available,
                loaded=_canonical(cfg.model) in loaded,
            )
            for role, cfg in self._config.roles.items()
        ]

    async def _names(self, path: str) -> set[str]:
        data = await self._request("GET", path, httpx.Timeout(_QUICK_TIMEOUT_S), None)
        models = data.get("models")
        if not isinstance(models, list):
            raise ProviderError("bad_model_output")
        names: set[str] = set()
        for entry in models:
            if isinstance(entry, dict):
                for key in ("name", "model"):
                    value = entry.get(key)
                    if isinstance(value, str):
                        names.add(_canonical(value))
        return names

    # ---- plumbing ------------------------------------------------------------------------

    async def _request(
        self, method: str, path: str, timeout: httpx.Timeout, body: _Json | None
    ) -> _Json:
        """One non-streamed call; returns the JSON object of a 200 reply.

        The HTTP timeouts only bound each wait for the next bytes, so the whole call also
        has a deadline: a reply that drips in slowly cannot hold the caller for ever.
        """
        deadline_s = (timeout.connect or 0.0) + (timeout.read or 0.0)
        try:
            async with (
                asyncio.timeout(deadline_s),
                self._client.stream(
                    method,
                    f"{self._base}{path}",
                    json=body,
                    headers=_HEADERS,
                    timeout=timeout,
                    follow_redirects=False,
                ) as response,
            ):
                _refuse_encoded(response)
                raw = await _read_capped(response)
                if response.status_code != httpx.codes.OK:
                    raise _status_error(response.status_code, raw)
                return _json_object(raw)
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from None
        except TimeoutError:
            raise ProviderError("model_timeout") from None

    def _role(self, role: Role) -> RoleConfig:
        cfg = self._config.roles.get(role)
        if cfg is None:
            raise ProviderError("model_missing")  # no model is configured for this role
        return cfg

    async def close(self) -> None:
        """Close the HTTP client, unless the caller supplied it."""
        if self._owns_client:
            await self._client.aclose()


# ---- reading untrusted replies -----------------------------------------------------------


def _refuse_encoded(response: httpx.Response) -> None:
    """Refuse a compressed reply before any of its body is read (see ``_HEADERS``)."""
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise ProviderError("bad_model_output")


async def _lines(response: httpx.Response) -> AsyncIterator[bytes]:
    """Split a streamed body into non-empty lines, with caps on each line and the total."""
    buffer = b""
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            raise ProviderError("too_large")
        buffer += chunk
        *complete, buffer = buffer.split(b"\n")
        if len(buffer) > MAX_LINE_BYTES or any(len(line) > MAX_LINE_BYTES for line in complete):
            raise ProviderError("too_large")
        for line in complete:
            if line.strip():
                yield line
    if buffer.strip():
        yield buffer


async def _read_capped(response: httpx.Response) -> bytes:
    body = b""
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise ProviderError("too_large")
    return body


def _json_object(raw: bytes) -> _Json:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):  # ValueError covers bad JSON and bad UTF-8
        raise ProviderError("bad_model_output") from None
    if not isinstance(data, dict):
        raise ProviderError("bad_model_output")
    return data


def _parse_chat_line(line: bytes) -> tuple[str, bool]:
    """One NDJSON line of a chat stream -> (reply text in it, is it the last line)."""
    data = _json_object(line)
    error = data.get("error")
    if isinstance(error, str):
        raise _message_error(error)
    done = data.get("done", False)
    if not isinstance(done, bool):
        raise ProviderError("bad_model_output")
    message = data.get("message", {})
    if not isinstance(message, dict):
        raise ProviderError("bad_model_output")
    content = message.get("content", "")
    if not isinstance(content, str):
        raise ProviderError("bad_model_output")
    return content, done


# ---- error mapping -----------------------------------------------------------------------


def _message_error(message: str, fallback: ErrorCode = "bad_model_output") -> ProviderError:
    """Classify a server error message. The text is only matched, never kept or logged."""
    text = message.lower()
    if "not found" in text:
        return ProviderError("model_missing")
    if "loading model" in text:
        # UNVERIFIED (spike S3.2): the exact wording Ollama uses while a runner starts.
        return ProviderError("model_timeout", waking=True)
    return ProviderError(fallback)


def _status_error(status: int, raw: bytes) -> ProviderError:
    try:
        message = _json_object(raw).get("error")
    except ProviderError:
        message = None
    text = message if isinstance(message, str) else ""
    if status == httpx.codes.NOT_FOUND:
        return ProviderError("model_missing")
    if 300 <= status < 400:
        return ProviderError("model_unreachable")  # whatever answered is not our server
    if status == httpx.codes.SERVICE_UNAVAILABLE:
        return _message_error(text, "busy")  # Ollama's queue is full
    if status == httpx.codes.BAD_REQUEST:
        return _message_error(text, "bad_request")
    return _message_error(text)


def _transport_error(exc: httpx.HTTPError) -> ProviderError:
    if isinstance(exc, httpx.ConnectTimeout):
        return ProviderError("model_unreachable")  # nothing is answering at that address
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("model_timeout")
    if isinstance(exc, httpx.TransportError):
        return ProviderError("model_unreachable")  # refused, DNS failure, connection dropped
    return ProviderError("bad_model_output")


def _canonical(name: str) -> str:
    """Model names compare equal with or without the implicit ``:latest`` tag, and in any
    letter case (Ollama itself matches names case-insensitively)."""
    name = name.lower()
    last = name.rsplit("/", 1)[-1]
    return name if ":" in last else f"{name}:latest"


def _ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
