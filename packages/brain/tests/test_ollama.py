"""OllamaProvider against a mock transport: exact requests, stream parsing, error mapping."""

from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import re
import tomllib
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

import waddle_brain.models as models_package
from waddle_brain.config import BrainConfig
from waddle_brain.contracts import ChatMessage, ProviderError
from waddle_brain.models import build_provider, ollama
from waddle_brain.models.ollama import OllamaProvider

FAST = "test-fast:1b"
SMART = "registry.test/team/test-smart"  # no tag: the server reports it as ":latest"
EMBED = "test-embed:tiny"

CONFIG = BrainConfig.model_validate(
    {
        "ollama_url": "http://ollama:11434",
        "roles": {
            "fast_vlm": {"model": FAST, "num_ctx": 4096, "keep_alive_s": 1800, "vision": True},
            "smart_model": {"model": SMART, "num_ctx": 16384, "keep_alive_s": 0},
            "embedder": {"model": EMBED, "num_ctx": 2048, "keep_alive_s": 60},
        },
        "limits": {"first_token_timeout_s": 7.0, "load_timeout_s": 33.0},
    }
)

Handler = Callable[[httpx.Request], httpx.Response]


def run[T](coro: Coroutine[object, object, T]) -> T:
    return asyncio.run(coro)


def provider_for(handler: Handler, **client_args: object) -> OllamaProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), **client_args)  # type: ignore[arg-type]
    return OllamaProvider(CONFIG, client=client)


def ndjson(*objects: dict[str, object]) -> bytes:
    return b"".join(json.dumps(obj).encode() + b"\n" for obj in objects)


def piece(text: str, *, done: bool = False) -> dict[str, object]:
    return {"message": {"role": "assistant", "content": text}, "done": done}


class Chunks(httpx.AsyncByteStream):
    """A body sent in exactly these chunks; optionally hangs or fails afterwards."""

    def __init__(self, *chunks: bytes, then: BaseException | str | None = None):
        self.chunks = chunks
        self.then = then
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.then == "hang":
            await asyncio.Event().wait()
        elif isinstance(self.then, BaseException):
            raise self.then

    async def aclose(self) -> None:
        self.closed = True


async def collect(provider: OllamaProvider, *args: object, **kwargs: object) -> list[str]:
    return [text async for text in provider.chat(*args, **kwargs)]  # type: ignore[arg-type]


def chat_error(handler: Handler, **chat_args: object) -> ProviderError:
    provider = provider_for(handler)
    with pytest.raises(ProviderError) as caught:
        run(collect(provider, "smart_model", [ChatMessage("user", "hi")], **chat_args))
    return caught.value


# ---- request shape -----------------------------------------------------------------------


def test_chat_sends_exactly_the_configured_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=ndjson(piece("ok", done=True)))

    provider = provider_for(handler)
    messages = [ChatMessage("system", "be brief"), ChatMessage("user", "what is this?")]
    assert run(collect(provider, "smart_model", messages)) == ["ok"]

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://ollama:11434/api/chat"
    assert json.loads(request.content) == {
        "model": SMART,
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "what is this?"},
        ],
        "stream": True,
        "keep_alive": 0,
        "options": {"num_ctx": 16384},
    }
    assert request.extensions["timeout"] == {"connect": 5.0, "read": 7.0, "write": 5.0, "pool": 5.0}


def test_chat_sends_images_as_base64_and_schema_as_format() -> None:
    class Verdict(BaseModel):
        ok: bool
        why: str

    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=ndjson(piece("{}", done=True)))

    image = b"\xff\xd8\xff\xe0 not really a jpeg \x00\x01"
    provider = provider_for(handler)
    run(collect(provider, "fast_vlm", [ChatMessage("user", "look", (image,))], schema=Verdict))

    assert seen == [
        {
            "model": FAST,
            "messages": [
                {"role": "user", "content": "look", "images": [base64.b64encode(image).decode()]}
            ],
            "stream": True,
            "keep_alive": 1800,
            "options": {"num_ctx": 4096},
            "format": Verdict.model_json_schema(),
        }
    ]


@pytest.mark.parametrize(
    ("role", "message"),
    [
        ("smart_model", ChatMessage("user", "look", (b"img",))),  # not a vision role
        ("fast_vlm", ChatMessage("system", "look", (b"img",))),  # images only on user turns
        ("embedder", ChatMessage("user", "hello")),  # an embedder cannot chat
    ],
)
def test_requests_the_role_cannot_serve_are_refused_before_any_network(
    role: str, message: ChatMessage
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing may be sent")

    provider = provider_for(handler)
    with pytest.raises(ProviderError) as caught:
        run(collect(provider, role, [message]))
    assert caught.value.code == "bad_request"


def test_load_and_unload_requests() -> None:
    seen: list[tuple[str, dict[str, object], object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.url.path, json.loads(request.content), request.extensions["timeout"]["read"])
        )
        return httpx.Response(200, json={"done": True, "done_reason": "load"})

    provider = provider_for(handler)

    async def go() -> None:
        await provider.load("fast_vlm")
        await provider.unload("fast_vlm")
        await provider.load("embedder")
        await provider.unload("embedder")

    run(go())
    assert seen == [
        (
            "/api/chat",
            {
                "model": FAST,
                "messages": [],
                "stream": False,
                "keep_alive": 1800,
                "options": {"num_ctx": 4096},
            },
            33.0,
        ),
        ("/api/chat", {"model": FAST, "messages": [], "stream": False, "keep_alive": 0}, 20.0),
        (
            "/api/embed",
            {"model": EMBED, "input": "a", "keep_alive": 60, "options": {"num_ctx": 2048}},
            33.0,
        ),
        ("/api/chat", {"model": EMBED, "messages": [], "stream": False, "keep_alive": 0}, 20.0),
    ]


def test_load_maps_a_missing_model_and_an_error_body() -> None:
    missing = provider_for(lambda _: httpx.Response(404, json={"error": "model 'x' not found"}))
    with pytest.raises(ProviderError) as caught:
        run(missing.load("smart_model"))
    assert caught.value.code == "model_missing"

    broken = provider_for(lambda _: httpx.Response(200, json={"error": "something broke"}))
    with pytest.raises(ProviderError) as caught:
        run(broken.load("smart_model"))
    assert caught.value.code == "bad_model_output"


# ---- stream parsing ----------------------------------------------------------------------


def test_stream_is_reassembled_across_chunk_boundaries() -> None:
    body = ndjson(piece("Hel"), piece("lo, "), piece("pingu é"), piece("", done=True))
    cut_a, cut_b = 10, len(body) - 30  # both cuts land inside a JSON line
    stream = Chunks(body[:cut_a], body[cut_a:cut_b], body[cut_b:])
    provider = provider_for(lambda _: httpx.Response(200, stream=stream))

    result = run(collect(provider, "smart_model", [ChatMessage("user", "hi")]))
    assert result == ["Hel", "lo, ", "pingu é"]
    assert stream.closed


def test_thinking_and_blank_lines_are_not_part_of_the_reply() -> None:
    body = (
        ndjson({"message": {"role": "assistant", "content": "", "thinking": "secret plan"}})
        + b"\n\r\n"
        + ndjson(piece("answer"), {"done": True, "done_reason": "stop"})
    )
    provider = provider_for(lambda _: httpx.Response(200, content=body))
    assert run(collect(provider, "smart_model", [ChatMessage("user", "hi")])) == ["answer"]


def test_nothing_after_the_done_line_is_read() -> None:
    body = ndjson(piece("a", done=True), piece("extra"))
    provider = provider_for(lambda _: httpx.Response(200, content=body))
    assert run(collect(provider, "smart_model", [ChatMessage("user", "hi")])) == ["a"]


@pytest.mark.parametrize(
    "body",
    [
        b"this is not json\n",
        b'["a list"]\n',
        b'{"message": "not an object", "done": false}\n',
        b'{"message": {"content": 5}, "done": false}\n',
        b'{"message": {"content": "x"}, "done": "yes"}\n',
        b'{"message": {"content": "\xff\xfe"}, "done": true}\n',
        ndjson(piece("cut off before done")),
        b"[" * 100_000 + b"\n",
    ],
)
def test_malformed_streams_are_bad_model_output(body: bytes) -> None:
    error = chat_error(lambda _: httpx.Response(200, content=body))
    assert error.code == "bad_model_output"


def test_an_overlong_line_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "MAX_LINE_BYTES", 200)
    unfinished = Chunks(b'{"message": {"content": "' + b"a" * 300, then="hang")
    assert chat_error(lambda _: httpx.Response(200, stream=unfinished)).code == "too_large"
    assert unfinished.closed

    finished = ndjson(piece("a" * 300), piece("", done=True))
    assert chat_error(lambda _: httpx.Response(200, content=finished)).code == "too_large"


def test_an_endless_stream_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "MAX_STREAM_BYTES", 1000)
    body = ndjson(*[piece("word ") for _ in range(100)])
    assert chat_error(lambda _: httpx.Response(200, content=body)).code == "too_large"


# ---- errors ------------------------------------------------------------------------------


def raising(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (raising(httpx.ConnectError("refused")), "model_unreachable"),
        (raising(httpx.ConnectTimeout("no answer")), "model_unreachable"),
        (raising(httpx.RemoteProtocolError("dropped")), "model_unreachable"),
        (raising(httpx.WriteTimeout("stuck")), "model_timeout"),
        (lambda _: httpx.Response(404, json={"error": "model 'x' not found"}), "model_missing"),
        (lambda _: httpx.Response(404, content=b"<html>"), "model_missing"),
        (lambda _: httpx.Response(500, json={"error": "model 'x' not found"}), "model_missing"),
        (lambda _: httpx.Response(400, json={"error": "invalid options"}), "bad_request"),
        (lambda _: httpx.Response(503, json={"error": "server busy"}), "busy"),
        (lambda _: httpx.Response(500, json={"error": "runner crashed"}), "bad_model_output"),
        (lambda _: httpx.Response(500, content=b"\x00garbage"), "bad_model_output"),
    ],
)
def test_failures_map_to_codes(handler: Handler, code: str) -> None:
    error = chat_error(handler)
    assert error.code == code
    assert not error.waking


def test_server_saying_it_is_loading_means_waking() -> None:
    error = chat_error(lambda _: httpx.Response(503, json={"error": "llm server loading model"}))
    assert (error.code, error.waking) == ("model_timeout", True)


def test_error_line_in_the_middle_of_a_stream() -> None:
    body = ndjson(piece("partial"), {"error": "an error was encountered while running the model"})
    provider = provider_for(lambda _: httpx.Response(200, content=body))
    got: list[str] = []

    async def go() -> None:
        async for text in provider.chat("smart_model", [ChatMessage("user", "hi")]):
            got.append(text)

    with pytest.raises(ProviderError) as caught:
        run(go())
    assert got == ["partial"]
    assert caught.value.code == "bad_model_output"


def test_error_line_saying_the_model_is_gone_is_model_missing() -> None:
    body = ndjson(piece("partial"), {"error": "model 'x' not found, try pulling it first"})
    assert chat_error(lambda _: httpx.Response(200, content=body)).code == "model_missing"


def timeout_handler(ps_models: list[dict[str, object]], *chunks: bytes) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": ps_models})
        return httpx.Response(200, stream=Chunks(*chunks, then=httpx.ReadTimeout("slow")))

    return handler


def test_timeout_before_any_text_is_waking_when_the_model_is_not_in_memory() -> None:
    error = chat_error(timeout_handler([]))
    assert (error.code, error.waking) == ("model_timeout", True)


def test_timeout_with_the_model_in_memory_is_a_plain_timeout() -> None:
    error = chat_error(timeout_handler([{"name": f"{SMART}:latest"}]))
    assert (error.code, error.waking) == ("model_timeout", False)


def test_timeout_after_text_started_is_never_waking() -> None:
    error = chat_error(timeout_handler([], ndjson(piece("some text"))))
    assert (error.code, error.waking) == ("model_timeout", False)


def test_redirects_are_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(307, headers={"Location": "http://elsewhere.test/api/chat"})

    # The client would follow redirects; the provider must forbid it on every request.
    provider = provider_for(handler, follow_redirects=True)

    async def go() -> list[str]:
        codes: list[str] = []
        for call in (
            collect(provider, "smart_model", [ChatMessage("user", "hi")]),
            provider.load("smart_model"),
            provider.unload("smart_model"),
            provider.status(),
        ):
            try:
                await call
            except ProviderError as exc:
                codes.append(exc.code)
        return codes

    assert run(go()) == ["model_unreachable"] * 4
    assert seen == ["ollama"] * 4


def test_own_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.test:3128")
    provider = build_provider(CONFIG)
    assert isinstance(provider, OllamaProvider)
    client = provider._client
    assert not client.trust_env
    assert not client.follow_redirects
    assert all(transport is None for transport in client._mounts.values())
    run(provider.close())
    assert client.is_closed


def test_close_leaves_a_supplied_client_open() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    run(OllamaProvider(CONFIG, client=client).close())
    assert not client.is_closed


# ---- cancellation ------------------------------------------------------------------------


def test_closing_the_generator_closes_the_http_stream() -> None:
    stream = Chunks(ndjson(piece("first")), then="hang")
    provider = provider_for(lambda _: httpx.Response(200, stream=stream))

    async def go() -> None:
        reply = provider.chat("smart_model", [ChatMessage("user", "hi")])
        assert await anext(reply) == "first"
        assert not stream.closed
        await reply.aclose()  # type: ignore[attr-defined]

    run(go())
    assert stream.closed


def test_cancelling_the_consumer_closes_the_http_stream() -> None:
    stream = Chunks(ndjson(piece("first")), then="hang")
    provider = provider_for(lambda _: httpx.Response(200, stream=stream))
    got: list[str] = []

    async def consume() -> None:
        async for text in provider.chat("smart_model", [ChatMessage("user", "hi")]):
            got.append(text)

    async def go() -> None:
        task = asyncio.create_task(consume())
        while not got:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert got == ["first"]
    assert stream.closed


# ---- status ------------------------------------------------------------------------------


def test_status_merges_tags_and_ps_per_role() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == "/api/tags":
            names = [FAST, f"{SMART}:latest", "someone-elses:model"]
            return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in names]})
        assert request.url.path == "/api/ps"
        entry = {"name": f"{SMART}:latest", "size": 10, "size_vram": 4}
        return httpx.Response(200, json={"models": [entry, "junk", {"name": 7}]})

    statuses = run(provider_for(handler).status())
    assert [(s.role, s.model, s.available, s.loaded) for s in statuses] == [
        ("fast_vlm", FAST, True, False),
        ("smart_model", SMART, True, True),
        ("embedder", EMBED, False, False),
    ]


def test_a_different_tag_of_the_same_model_does_not_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": FAST.replace(":1b", ":2b")}]})

    statuses = run(provider_for(handler).status())
    assert not any(s.available or s.loaded for s in statuses)


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (raising(httpx.ConnectError("refused")), "model_unreachable"),
        (raising(httpx.ReadTimeout("slow")), "model_timeout"),
        (lambda _: httpx.Response(200, json={"models": "nope"}), "bad_model_output"),
        (lambda _: httpx.Response(200, content=b"<html>"), "bad_model_output"),
    ],
)
def test_status_failures(handler: Handler, code: str) -> None:
    with pytest.raises(ProviderError) as caught:
        run(provider_for(handler).status())
    assert caught.value.code == code


def test_an_oversized_status_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "MAX_BODY_BYTES", 100)
    body = json.dumps({"models": [{"name": "x" * 200}]}).encode()
    with pytest.raises(ProviderError) as caught:
        run(provider_for(lambda _: httpx.Response(200, content=body)).status())
    assert caught.value.code == "too_large"


# ---- hygiene -----------------------------------------------------------------------------


def test_logs_carry_no_content(logs: io.StringIO) -> None:
    secret_reply = "the-reply-text-that-must-not-be-logged"
    secret_error = "error-text-that-must-not-be-logged"

    def handler(request: httpx.Request) -> httpx.Response:
        if b"fail" in request.content:
            return httpx.Response(500, json={"error": secret_error})
        return httpx.Response(200, content=ndjson(piece(secret_reply, done=True)))

    provider = provider_for(handler)
    run(collect(provider, "fast_vlm", [ChatMessage("user", "question-not-to-log")]))
    with pytest.raises(ProviderError):
        run(collect(provider, "fast_vlm", [ChatMessage("user", "fail")]))

    text = logs.getvalue()
    assert "model_chat_done" in text
    assert "model_chat_failed" in text
    for forbidden in (secret_reply, secret_error, "question-not-to-log", FAST):
        assert forbidden not in text


def test_no_model_name_appears_in_the_models_package() -> None:
    package_dir = Path(models_package.__file__).parent
    repo = next(p for p in package_dir.parents if (p / "deploy" / "brain.toml").is_file())
    shipped = tomllib.loads((repo / "deploy" / "brain.toml").read_text(encoding="utf-8"))
    configured = {role["model"].lower() for role in shipped["roles"].values()}
    families = {name.split(":")[0] for name in configured} | {
        re.sub(r"[\d.]+$", "", name.split(":")[0].split("-")[0]) for name in configured
    }
    # Common model families, so a name is caught even after the shipped config changes.
    families |= {"llama", "llava", "gemma", "mistral", "qwen", "phi3", "phi4", "deepseek"}
    families |= {"minilm", "nomic", "venus", "moondream", "granite", "smolvlm"}
    pattern = re.compile(r"(?<![a-z])(" + "|".join(sorted(map(re.escape, families))) + ")")

    sources = sorted(package_dir.glob("**/*.py"))
    assert len(sources) >= 4
    for source in sources:
        found = pattern.findall(source.read_text(encoding="utf-8").lower())
        assert not found, f"{source.name} names a model: {found}"


# ---- review additions --------------------------------------------------------------------


def test_text_that_cannot_be_encoded_is_a_bad_request_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing may be sent")

    lone_surrogate = json.loads('"\\ud800"')  # what a JSON escape can smuggle in
    provider = provider_for(handler)
    with pytest.raises(ProviderError) as caught:
        run(collect(provider, "smart_model", [ChatMessage("user", f"hi {lone_surrogate}")]))
    assert caught.value.code == "bad_request"


def test_compressed_replies_are_not_asked_for_and_not_decoded() -> None:
    # A compressed body can expand far past the size caps inside the HTTP library, before
    # this module gets to count it.
    seen: list[str | None] = []
    bomb = gzip.compress(ndjson(piece("x" * 100_000, done=True)))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, stream=Chunks(bomb), headers={"Content-Encoding": "gzip"})

    provider = provider_for(handler)

    async def go() -> list[str]:
        codes: list[str] = []
        for call in (
            collect(provider, "smart_model", [ChatMessage("user", "hi")]),
            provider.load("smart_model"),
            provider.status(),
        ):
            try:
                await call
            except ProviderError as exc:
                codes.append(exc.code)
        return codes

    assert run(go()) == ["bad_model_output"] * 3
    assert seen == ["identity"] * 3


def test_a_dripping_reply_cannot_hold_a_status_call_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each chunk arrives inside the read timeout, so only an overall deadline ends this.
    monkeypatch.setattr(ollama, "_QUICK_TIMEOUT_S", 0.05)

    class Drip(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(0.01)
                yield b" "

        async def aclose(self) -> None:
            self.closed = True

    drip = Drip()
    provider = provider_for(lambda _: httpx.Response(200, stream=drip))

    async def go() -> str:
        try:
            await asyncio.wait_for(provider.status(), 2)
        except ProviderError as exc:
            return exc.code
        return "no error"

    assert run(go()) == "model_timeout"
    assert drip.closed


def test_cancelling_a_status_call_is_not_turned_into_a_timeout() -> None:
    stream = Chunks(then="hang")
    provider = provider_for(lambda _: httpx.Response(200, stream=stream))

    async def go() -> None:
        task = asyncio.create_task(provider.status())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert stream.closed


def test_a_role_without_a_configured_model_is_missing() -> None:
    config = CONFIG.model_copy(
        update={"roles": {k: v for k, v in CONFIG.roles.items() if k != "smart_model"}}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"models": []})
        raise AssertionError("nothing may be sent for an unconfigured role")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaProvider(config, client=client)

    async def go() -> list[str]:
        codes: list[str] = []
        for call in (
            collect(provider, "smart_model", [ChatMessage("user", "hi")]),
            provider.load("smart_model"),
            provider.unload("smart_model"),
        ):
            try:
                await call
            except ProviderError as exc:
                codes.append(exc.code)
        return codes

    assert run(go()) == ["model_missing"] * 3
    assert [s.role for s in run(provider.status())] == ["fast_vlm", "embedder"]


def test_status_name_matching_handles_registry_ports_and_letter_case() -> None:
    config = BrainConfig.model_validate(
        {
            "roles": {
                "fast_vlm": {"model": "registry.test:5000/team/Test-Fast"},
                "smart_model": {"model": "Test-Smart:Q4"},
            }
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        names = ["registry.test:5000/team/test-fast:latest", "test-smart:q4"]
        return httpx.Response(200, json={"models": [{"name": n} for n in names]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    statuses = run(OllamaProvider(config, client=client).status())
    assert [(s.available, s.loaded) for s in statuses] == [(True, True), (True, True)]
