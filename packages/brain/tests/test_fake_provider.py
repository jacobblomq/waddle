"""FakeProvider: scripted replies, recorded calls, and the failures it can act out."""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel

from waddle_brain.contracts import ChatMessage, ModelProvider, ProviderError
from waddle_brain.models.fake import DEFAULT_REPLY, FakeCall, FakeProvider, FakeReply

ASK = (ChatMessage("user", "hi"),)


async def collect(provider: FakeProvider, role: str = "smart_model") -> list[str]:
    return [text async for text in provider.chat(role, ASK)]  # type: ignore[arg-type]


def test_it_satisfies_the_provider_protocol() -> None:
    provider: ModelProvider = FakeProvider()
    assert asyncio.run(provider.status())


def test_unscripted_role_gives_the_default_reply() -> None:
    assert asyncio.run(collect(FakeProvider())) == [DEFAULT_REPLY]


def test_scripted_replies_come_in_order_and_the_last_repeats() -> None:
    provider = FakeProvider()
    provider.script("smart_model", "one", ["two ", "pieces"], FakeReply(("three",)))
    provider.script("fast_vlm", "other role")

    async def go() -> list[list[str]]:
        return [await collect(provider) for _ in range(4)] + [await collect(provider, "fast_vlm")]

    assert asyncio.run(go()) == [["one"], ["two ", "pieces"], ["three"], ["three"], ["other role"]]


def test_calls_are_recorded() -> None:
    class Shape(BaseModel):
        n: int

    provider = FakeProvider()

    async def go() -> None:
        await provider.load("fast_vlm")
        async for _ in provider.chat("fast_vlm", ASK, schema=Shape):
            pass
        await provider.unload("fast_vlm")
        await provider.status()
        await provider.close()

    asyncio.run(go())
    assert provider.calls == [
        FakeCall("load", "fast_vlm"),
        FakeCall("chat", "fast_vlm", ASK, Shape),
        FakeCall("unload", "fast_vlm"),
        FakeCall("status"),
        FakeCall("close"),
    ]
    assert provider.closed


def test_status_follows_load_chat_unload_and_missing() -> None:
    provider = FakeProvider(roles=("fast_vlm", "smart_model"))
    provider.missing.add("smart_model")

    async def flags() -> list[tuple[str, bool, bool]]:
        return [(s.role, s.available, s.loaded) for s in await provider.status()]

    async def go() -> None:
        assert await flags() == [("fast_vlm", True, False), ("smart_model", False, False)]
        await provider.load("fast_vlm")
        assert await flags() == [("fast_vlm", True, True), ("smart_model", False, False)]
        await provider.unload("fast_vlm")
        await collect(provider, "fast_vlm")
        assert await flags() == [("fast_vlm", True, True), ("smart_model", False, False)]
        await provider.unload("fast_vlm")
        assert await flags() == [("fast_vlm", True, False), ("smart_model", False, False)]

    asyncio.run(go())


def test_missing_model_fails_chat_and_load() -> None:
    provider = FakeProvider()
    provider.missing.add("smart_model")
    with pytest.raises(ProviderError) as chat_error:
        asyncio.run(collect(provider))
    with pytest.raises(ProviderError) as load_error:
        asyncio.run(provider.load("smart_model"))
    assert chat_error.value.code == load_error.value.code == "model_missing"
    assert "smart_model" not in provider.loaded


def test_waking_failure_before_any_text() -> None:
    provider = FakeProvider()
    provider.script("smart_model", FakeReply(error="model_timeout", waking=True), "awake now")
    with pytest.raises(ProviderError) as caught:
        asyncio.run(collect(provider))
    assert (caught.value.code, caught.value.waking) == ("model_timeout", True)
    assert asyncio.run(collect(provider)) == ["awake now"]


def test_failure_in_the_middle_of_a_stream() -> None:
    provider = FakeProvider()
    provider.script("smart_model", FakeReply(("so far ", "so good"), error="model_unreachable"))
    got: list[str] = []

    async def go() -> None:
        async for text in provider.chat("smart_model", ASK):
            got.append(text)

    with pytest.raises(ProviderError) as caught:
        asyncio.run(go())
    assert got == ["so far ", "so good"]
    assert (caught.value.code, caught.value.waking) == ("model_unreachable", False)


def test_delay_is_waited_before_each_piece_and_can_be_cancelled() -> None:
    provider = FakeProvider()
    provider.script(
        "smart_model", FakeReply(("a", "b"), delay_s=0.05), FakeReply(("x",), delay_s=30)
    )

    started = time.monotonic()
    assert asyncio.run(collect(provider)) == ["a", "b"]
    assert time.monotonic() - started >= 0.1

    async def cancelled() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(collect(provider), 0.05)

    asyncio.run(cancelled())
