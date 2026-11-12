"""The capture client against an in-process fake helper speaking PROTOCOL.md over a socketpair.

pytest-asyncio is not available: every test drives its own loop with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from waddle_desktop.sensors.capture_client import (
    MAX_FRAME,
    Blind,
    CaptureClient,
    CaptureClosedError,
    CaptureError,
    CaptureProtocolError,
    CaptureRequestError,
    CaptureTimeoutError,
    Frame,
    Hello,
    Unchanged,
)

UUID = "0a1b2c3d-0000-4000-8000-00000000abcd"
POPUP = "{11111111-2222-4333-8444-555555555555}"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 60
HASH = "8f3c00a1b2c3d4e5"


def frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def header(
    request: dict[str, Any], status: str, frames: list[int] | None = None, **fields: Any
) -> bytes:
    body = {"id": request["id"], "status": status, "frames": frames or [], **fields}
    return frame(json.dumps(body).encode())


def ok_capture(request: dict[str, Any], **overrides: Any) -> bytes:
    fields: dict[str, Any] = {
        "hash": HASH,
        "scale": 1.5,
        "width": 2880,
        "height": 1620,
        "overview": {"w": 1024, "h": 576, "format": "jpeg"},
        "crop": {"x": 1200, "y": 400, "w": 512, "h": 512, "format": "png"},
        "popups": 1,
        "elapsed_ms": 84,
    }
    fields.update(overrides)
    return header(request, "ok", [len(JPEG), len(PNG)], **fields) + frame(JPEG) + frame(PNG)


Responder = Callable[[dict[str, Any]], bytes | None]


class FakeHelper:
    """Reads framed requests and answers each with whatever bytes ``responder`` returns.

    ``None`` means "say nothing" (for timeouts). The raw bytes are written as they are, so a
    test can send any malformed reply it likes.
    """

    def __init__(self, sock: socket.socket, responder: Responder) -> None:
        self.sock = sock
        self.responder = responder
        self.requests: list[dict[str, Any]] = []
        self.pipelined = False
        self.close_after_reply = False

    async def run(self) -> None:
        reader, writer = await asyncio.open_connection(sock=self.sock)
        try:
            while True:
                try:
                    (length,) = struct.unpack(">I", await reader.readexactly(4))
                except asyncio.IncompleteReadError:
                    return
                request = json.loads(await reader.readexactly(length))
                self.requests.append(request)
                # A well-behaved client sends nothing more until it has the whole reply.
                await asyncio.sleep(0.03)
                self.pipelined |= len(reader._buffer) > 0  # type: ignore[attr-defined]
                reply = self.responder(request)
                if reply is not None:
                    writer.write(reply)
                    await writer.drain()
                if self.close_after_reply:
                    return
        finally:
            writer.close()


def run(
    responder: Responder,
    scenario: Callable[[CaptureClient, FakeHelper], Awaitable[Any]],
    *,
    timeout: float = 5.0,
    close_after_reply: bool = False,
) -> FakeHelper:
    """Run ``scenario`` against a fake helper; fails the test rather than hanging."""

    async def main() -> FakeHelper:
        ours, theirs = socket.socketpair()
        helper = FakeHelper(theirs, responder)
        helper.close_after_reply = close_after_reply
        task = asyncio.create_task(helper.run())
        client = CaptureClient(ours, timeout=timeout)
        try:
            await asyncio.wait_for(scenario(client, helper), 10)
        finally:
            client.close()
            await asyncio.wait_for(task, 5)
        return helper

    return asyncio.run(main())


def expect_failure(
    reply: Callable[[dict[str, Any]], bytes | None],
    error: type[Exception],
    *,
    close_after_reply: bool = False,
    timeout: float = 5.0,
) -> None:
    """One capture answered with ``reply`` must raise ``error`` and close the client."""

    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        with pytest.raises(error):
            await client.capture_window(UUID)
        assert client.closed, "a broken stream must not be reused"
        with pytest.raises(CaptureClosedError):
            await client.capture_window(UUID)

    started = time.monotonic()
    run(reply, scenario, close_after_reply=close_after_reply, timeout=timeout)
    assert time.monotonic() - started < 4, "the failure must be prompt, not a hang"


# ------------------------------------------------------------------ happy paths


def test_hello_is_parsed() -> None:
    def responder(request: dict[str, Any]) -> bytes:
        return header(
            request,
            "ok",
            version="0.2.0",
            protocol=1,
            kwin=True,
            screenshot2_version=5,
            permission_regime="restricted",
            lock_check=True,
            config_ok=False,
            latched=False,
        )

    result: list[Hello] = []

    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        result.append(await client.hello())

    helper = run(responder, scenario)
    assert helper.requests == [{"id": 1, "verb": "hello"}]
    assert result == [
        Hello(
            version="0.2.0",
            protocol=1,
            kwin=True,
            screenshot2_version=5,
            permission_regime="restricted",
            lock_check=True,
            config_ok=False,
            latched=False,
        )
    ]


def test_hello_without_screenshot2() -> None:
    def responder(request: dict[str, Any]) -> bytes:
        return header(
            request,
            "ok",
            version="0.2.0",
            protocol=1,
            kwin=False,
            screenshot2_version=None,
            permission_regime="unknown",
            lock_check=False,
            config_ok=True,
            latched=True,
        )

    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        hello = await client.hello()
        assert hello.screenshot2_version is None
        assert (hello.kwin, hello.lock_check, hello.latched) == (False, False, True)

    run(responder, scenario)


def test_capture_frame_round_trip_and_request_shape() -> None:
    result: list[object] = []

    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        result.append(
            await client.capture_window(
                UUID,
                popups=[POPUP],
                roi=(120, 40, 300, 200),
                overview_max=800,
                crop_size=256,
                live_regions=[(0, 0, 640, 360)],
                force=True,
            )
        )

    helper = run(ok_capture, scenario)
    assert helper.requests == [
        {
            "id": 1,
            "verb": "capture_window",
            "uuid": UUID,
            "popups": [POPUP],
            "roi": {"x": 120.0, "y": 40.0, "w": 300.0, "h": 200.0},
            "overview_max": 800,
            "crop_size": 256,
            "live_regions": [{"x": 0.0, "y": 0.0, "w": 640.0, "h": 360.0}],
            "force": True,
        }
    ]
    assert result == [
        Frame(
            hash=HASH,
            scale=1.5,
            width=2880,
            height=1620,
            overview=JPEG,
            overview_size=(1024, 576),
            crop=PNG,
            crop_rect=(1200, 400, 512, 512),
            elapsed_ms=84,
            popups=1,
        )
    ]


def test_capture_defaults_and_cursor() -> None:
    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        await client.capture_window(UUID)
        await client.capture_window(UUID, cursor=(10, 20.5))

    helper = run(ok_capture, scenario)
    assert helper.requests[0] == {
        "id": 1,
        "verb": "capture_window",
        "uuid": UUID,
        "overview_max": 1024,
        "crop_size": 512,
        "force": False,
    }
    assert helper.requests[1]["cursor"] == {"x": 10.0, "y": 20.5}
    assert helper.requests[1]["id"] == 2, "ids increase"
    assert "roi" not in helper.requests[1]


@pytest.mark.parametrize(
    ("status", "fields", "expected"),
    [
        ("unchanged", {"hash": HASH}, Unchanged(hash=HASH)),
        ("blind", {"reason": "locked"}, Blind(reason="locked")),
        ("blind", {"reason": "blocked"}, Blind(reason="blocked")),
        ("blind", {"reason": "latched"}, Blind(reason="latched")),
        ("blind", {"reason": "not_found"}, Blind(reason="not_found")),
        (
            "error",
            {"code": "not_authorized", "message": "KWin said no"},
            CaptureError(code="not_authorized", message="KWin said no"),
        ),
    ],
)
def test_capture_results_without_images(
    status: str, fields: dict[str, Any], expected: object
) -> None:
    result: list[object] = []

    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        result.append(await client.capture_window(UUID))
        assert not client.closed, "these are normal answers, the client stays usable"

    run(lambda request: header(request, status, **fields), scenario)
    assert result == [expected]


def test_control_verbs() -> None:
    def responder(request: dict[str, Any]) -> bytes:
        if request["verb"] == "blind":
            return header(request, "ok", latched=request["on"])
        return header(request, "ok")

    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        await client.set_blind(True)
        await client.set_blind(False)
        await client.forget()
        await client.ping()

    helper = run(responder, scenario)
    assert helper.requests == [
        {"id": 1, "verb": "blind", "on": True},
        {"id": 2, "verb": "blind", "on": False},
        {"id": 3, "verb": "forget"},
        {"id": 4, "verb": "ping"},
    ]


def test_blind_must_be_acknowledged_with_the_requested_state() -> None:
    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        with pytest.raises(CaptureProtocolError):
            await client.set_blind(True)
        assert client.closed

    run(lambda request: header(request, "ok", latched=False), scenario)


def test_control_request_error_is_typed_and_not_fatal() -> None:
    replies = iter(["error", "ok"])

    def responder(request: dict[str, Any]) -> bytes:
        if next(replies) == "error":
            return header(request, "error", code="bad_request", message="nope")
        return header(request, "ok")

    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        with pytest.raises(CaptureRequestError) as caught:
            await client.forget()
        assert (caught.value.code, caught.value.message) == ("bad_request", "nope")
        await client.forget()

    run(responder, scenario)


def test_a_frame_overtaken_by_blind_is_discarded() -> None:
    """Blind means blind (I4): going blind while a capture is in flight discards its frame.

    Requests are serialized, so the helper only latches after it has answered the capture.
    Without this the caller would be handed pixels after it had decided to stop looking.
    """

    def responder(request: dict[str, Any]) -> bytes:
        if request["verb"] == "blind":
            return header(request, "ok", [], latched=request["on"])
        return ok_capture(request)

    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        in_flight = asyncio.create_task(client.capture_window(UUID))
        queued = asyncio.create_task(client.capture_window(UUID))
        await asyncio.sleep(0.01)  # the first request is on the wire, its reply is not
        await client.set_blind(True)
        assert await in_flight == Blind("latched")
        await client.set_blind(False)
        # Asked for before the blind, answered after it was lifted: still not handed over.
        assert await queued == Blind("latched")
        assert isinstance(await client.capture_window(UUID), Frame)
        # Lifting the latch discards nothing.
        later = asyncio.create_task(client.capture_window(UUID))
        await asyncio.sleep(0.01)
        await client.set_blind(False)
        assert isinstance(await later, Frame)

    helper = run(responder, scenario)
    assert not helper.pipelined


def test_requests_are_serialized_one_in_flight() -> None:
    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        results = await asyncio.gather(*(client.capture_window(UUID) for _ in range(4)))
        assert all(isinstance(r, Frame) for r in results)

    helper = run(ok_capture, scenario)
    assert [r["id"] for r in helper.requests] == [1, 2, 3, 4]
    assert not helper.pipelined, "a request was sent before the previous reply was complete"


# ------------------------------------------------------------------ argument validation


@pytest.mark.parametrize(
    "kwargs",
    [
        {"uuid": "active"},
        {"uuid": ""},
        {"uuid": UUID + "0"},
        {"uuid": UUID + "\n"},
        {"uuid": UUID, "popups": [UUID + "\n"]},
        {"uuid": UUID, "popups": ["nope"]},
        {"uuid": UUID, "roi": (1, 2, 3, 4), "cursor": (1, 2)},
        {"uuid": UUID, "roi": (1, 2, 3)},
        {"uuid": UUID, "live_regions": [(1, 2)]},
    ],
)
def test_bad_arguments_raise_before_anything_is_sent(kwargs: dict[str, Any]) -> None:
    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        with pytest.raises(ValueError):
            await client.capture_window(**kwargs)
        assert not client.closed

    helper = run(ok_capture, scenario)
    assert helper.requests == []


# ------------------------------------------------------------------ hostile or broken replies


def test_oversize_frame_is_refused_without_reading_it() -> None:
    expect_failure(lambda _r: struct.pack(">I", MAX_FRAME + 1) + b"x" * 64, CaptureProtocolError)


def test_oversize_header_is_refused() -> None:
    expect_failure(lambda _r: frame(b" " * (64 * 1024 + 1)), CaptureProtocolError)


def test_oversize_binary_frame_is_refused() -> None:
    def reply(request: dict[str, Any]) -> bytes:
        return header(request, "ok", [MAX_FRAME + 1, 10], hash=HASH)

    expect_failure(reply, CaptureProtocolError)


def test_truncated_reply_does_not_hang() -> None:
    # The header announces two images; the helper dies after half of the first.
    def reply(request: dict[str, Any]) -> bytes:
        return ok_capture(request)[: -(len(PNG) + 4 + len(JPEG) // 2)]

    expect_failure(reply, CaptureClosedError, close_after_reply=True)


def test_truncated_header_does_not_hang() -> None:
    expect_failure(
        lambda _r: struct.pack(">I", 100) + b'{"id":', CaptureClosedError, close_after_reply=True
    )


def test_helper_closing_without_a_reply() -> None:
    expect_failure(lambda _r: None, CaptureClosedError, close_after_reply=True)


def test_silent_helper_times_out() -> None:
    started = time.monotonic()
    expect_failure(lambda _r: None, CaptureTimeoutError, timeout=0.2)
    assert time.monotonic() - started >= 0.2


def test_stalled_mid_reply_times_out() -> None:
    def reply(request: dict[str, Any]) -> bytes:
        return ok_capture(request)[:-10]

    expect_failure(reply, CaptureTimeoutError, timeout=0.2)


def test_mismatched_id_is_a_protocol_error() -> None:
    def reply(request: dict[str, Any]) -> bytes:
        return header({"id": request["id"] + 1}, "blind", reason="locked")

    expect_failure(reply, CaptureProtocolError)


@pytest.mark.parametrize(
    "bad_id",
    [None, "1", 1.5, True],
)
def test_wrongly_typed_id_is_a_protocol_error(bad_id: object) -> None:
    def reply(_request: dict[str, Any]) -> bytes:
        return frame(
            json.dumps({"id": bad_id, "status": "blind", "reason": "locked", "frames": []}).encode()
        )

    expect_failure(reply, CaptureProtocolError)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json at all",
        b"\xff\xfe\x00",
        b"[1, 2, 3]",
        b'"ok"',
        b"",
        b"[" * 100_000,
    ],
)
def test_header_that_is_not_a_json_object(payload: bytes) -> None:
    expect_failure(lambda _r: frame(payload[: 64 * 1024]), CaptureProtocolError)


def _mutated(mutate: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], bytes]:
    """An otherwise valid ok-capture reply with one thing wrong in its header."""

    def reply(request: dict[str, Any]) -> bytes:
        good = ok_capture(request)
        (length,) = struct.unpack(">I", good[:4])
        body = json.loads(good[4 : 4 + length])
        mutate(body)
        return frame(json.dumps(body).encode()) + good[4 + length :]

    return reply


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h.update(status="fine"),
        lambda h: h.pop("status"),
        lambda h: h.pop("frames"),
        lambda h: h.update(frames="two"),
        lambda h: h.update(frames=[len(JPEG), -1]),
        lambda h: h.update(frames=[len(JPEG), True]),
        lambda h: h.update(frames=[len(JPEG), len(PNG), 5]),
        lambda h: h.update(frames=[len(JPEG)]),
        lambda h: h.update(frames=[len(JPEG) + 1, len(PNG)]),
        lambda h: h.update(frames=[len(JPEG) - 1, len(PNG)]),
        lambda h: h.pop("hash"),
        lambda h: h.update(hash="xyz"),
        lambda h: h.update(hash=12),
        lambda h: h.update(scale=0),
        lambda h: h.update(scale="1.5"),
        lambda h: h.update(scale=True),
        lambda h: h.update(width=0),
        lambda h: h.update(width="2880"),
        lambda h: h.update(height=2**40),
        lambda h: h.update(overview=None),
        lambda h: h.update(overview={"w": 1024, "h": 576, "format": "png"}),
        lambda h: h.update(overview={"w": 4000, "h": 576, "format": "jpeg"}),
        lambda h: h.update(crop={"x": 2600, "y": 400, "w": 512, "h": 512, "format": "png"}),
        lambda h: h.update(crop={"x": -1, "y": 400, "w": 512, "h": 512, "format": "png"}),
        lambda h: h.update(crop={"x": 0, "y": 0, "w": 512, "format": "png"}),
        lambda h: h.update(crop=[0, 0, 512, 512]),
        lambda h: h.update(elapsed_ms=-5),
        lambda h: h.update(popups="many"),
    ],
)
def test_malformed_capture_header_is_a_protocol_error(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    expect_failure(_mutated(mutate), CaptureProtocolError, timeout=1.0)


def test_images_must_look_like_their_announced_format() -> None:
    def reply(request: dict[str, Any]) -> bytes:
        good = ok_capture(request)
        return good.replace(JPEG, b"B" * len(JPEG))

    expect_failure(reply, CaptureProtocolError)


@pytest.mark.parametrize(
    ("status", "fields"),
    [
        ("blind", {"reason": "because"}),
        ("blind", {}),
        ("blind", {"reason": 7}),
        ("unchanged", {}),
        ("unchanged", {"hash": "ABCDEF0123456789"}),
        ("error", {"code": "Has Spaces", "message": "x"}),
        ("error", {"code": "bad_reply"}),
        ("error", {"code": "bad_reply", "message": "x" * 301}),
    ],
)
def test_malformed_imageless_replies(status: str, fields: dict[str, Any]) -> None:
    expect_failure(lambda request: header(request, status, **fields), CaptureProtocolError)


def test_images_attached_to_a_blind_reply_are_refused_unread() -> None:
    def reply(request: dict[str, Any]) -> bytes:
        return header(request, "blind", [len(JPEG)], reason="locked") + frame(JPEG)

    expect_failure(reply, CaptureProtocolError)


def test_malformed_hello_closes_the_client() -> None:
    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        with pytest.raises(CaptureProtocolError):
            await client.hello()
        assert client.closed

    run(lambda request: header(request, "ok", version="0.2.0", protocol=1, kwin="yes"), scenario)


# ------------------------------------------------------------------ lifecycle


def test_close_is_idempotent_and_ends_the_connection() -> None:
    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        await client.ping()
        client.close()
        client.close()
        with pytest.raises(CaptureClosedError):
            await client.ping()

    helper = run(lambda request: header(request, "ok"), scenario)
    assert len(helper.requests) == 1


def test_close_before_first_use_closes_the_socket() -> None:
    ours, theirs = socket.socketpair()
    with theirs:
        CaptureClient(ours).close()
        assert ours.fileno() == -1
        assert theirs.recv(1) == b"", "the helper side sees end-of-stream"


def test_cancelled_request_closes_the_client() -> None:
    async def scenario(client: CaptureClient, _helper: FakeHelper) -> None:
        task = asyncio.create_task(client.capture_window(UUID))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed, "a half-read reply must not be mistaken for the next one"

    run(lambda _r: None, scenario)


def test_invalid_timeout_is_rejected() -> None:
    ours, theirs = socket.socketpair()
    with ours, theirs, pytest.raises(ValueError):
        CaptureClient(ours, timeout=0)


# ------------------------------------------------------------------ against the real helper


def _built_helper() -> Path | None:
    root = Path(__file__).resolve().parents[3] / "capture-helper" / "target"
    candidates = [root / "release" / "waddle-capture", root / "debug" / "waddle-capture"]
    built = [p for p in candidates if p.is_file()]
    return max(built, key=lambda p: p.stat().st_mtime) if built else None


def test_real_helper_speaks_the_same_protocol(tmp_path: Path) -> None:
    """Protocol drift check. No session is needed: without KWin the helper still answers."""
    binary = _built_helper()
    if binary is None:
        pytest.skip("waddle-capture is not built (cargo build in capture-helper/)")
    ours, theirs = socket.socketpair()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path}/no-such-bus",
        "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={tmp_path}/no-such-bus",
    }
    process = subprocess.Popen(
        [str(binary), "--fd", str(theirs.fileno()), "--idle-timeout", "30"],
        pass_fds=(theirs.fileno(),),
        env=env,
        stdin=subprocess.DEVNULL,
    )
    theirs.close()

    async def scenario() -> None:
        client = CaptureClient(ours, timeout=8)
        try:
            hello = await client.hello()
            assert hello.protocol == 1
            assert (hello.kwin, hello.screenshot2_version, hello.lock_check) == (False, None, False)
            assert hello.permission_regime == "unknown"
            await client.ping()
            await client.forget()
            result = await client.capture_window(UUID, popups=[POPUP], roi=(1, 2, 3, 4))
            assert result == CaptureError(
                code="no_capture_path", message="org.kde.KWin.ScreenShot2 is not available"
            )
            await client.set_blind(True)
            assert await client.capture_window(UUID, force=True) == Blind(reason="latched")
            await client.set_blind(False)
        finally:
            client.close()

    try:
        asyncio.run(scenario())
        assert process.wait(timeout=5) == 0, "the helper exits when the socket closes"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
