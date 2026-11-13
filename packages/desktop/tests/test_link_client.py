"""The host link against a real WebSocket server on a real Unix socket (``fake_brain``)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fake_brain as fb

from waddle_core.link import (
    AnswerDelta,
    AnswerDone,
    AnswerError,
    AppContext,
    Ask,
    Cancel,
    Prewarm,
    State,
    ToolCall,
    ToolResult,
    Welcome,
    WindowContext,
)
from waddle_core.tools import MAX_TOOL_CALLS_PER_REQUEST
from waddle_desktop.link import client as client_mod
from waddle_desktop.link.client import BrainDownError, BrainLink, LinkSocket, LinkState
from waddle_desktop.link.tools_host import HostToolDispatcher

TOKEN = "c0ffee" + "ab" * 29
CANARY = "CANARY-51d9"
JPEG = b"\xff\xd8" + b"j" * 5000
PNG = b"\x89PNG" + b"p" * 300


class Backends:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.gate: asyncio.Event | None = None  # set to hold tools until released

    async def _answer(self, *call: str) -> dict[str, object]:
        self.calls.append(call)
        if self.gate is not None:
            await self.gate.wait()
        return {"ok": call[0]}

    async def app_state(self) -> dict[str, object]:
        return await self._answer("app_state")

    async def system_stats(self, topic: str) -> dict[str, object]:
        return await self._answer("system_stats", topic)

    async def man(self, name: str, section: str | None) -> dict[str, object]:
        return await self._answer("man", name, section or "")

    async def tldr(self, name: str) -> dict[str, object]:
        return await self._answer("tldr", name)


class Sleeper:
    """Stands in for ``asyncio.sleep``: records each delay and waits only until released."""

    def __init__(self, *, instant: bool = False):
        self.delays: list[float] = []
        self.instant = instant
        self._release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.instant:
            await asyncio.sleep(0)
            return
        self._release.clear()
        await self._release.wait()

    def release(self) -> None:
        self._release.set()


class Answer:
    """Collects one request's callbacks, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def kwargs(self) -> dict[str, Callable[..., None]]:
        return {
            "on_delta": lambda text: self.events.append(("delta", text)),
            "on_done": lambda done: self.events.append(("done", done)),
            "on_error": lambda code: self.events.append(("error", code)),
        }

    @property
    def ended(self) -> list[tuple[str, object]]:
        return [e for e in self.events if e[0] != "delta"]


class Rig:
    """A runtime directory, a token file, a fake brain and a link wired to them."""

    def __init__(self, root: Path):
        self.dir = root / "run"
        self.dir.mkdir(mode=0o700)
        self.sock = self.dir / "brain.sock"
        self.token_file = root / "link-token"
        self.token_file.write_text(TOKEN)
        self.token_file.chmod(0o600)
        self.brain = fb.FakeBrain(self.sock, TOKEN)
        self.backends = Backends()
        self.states: list[LinkState] = []
        self.sleeper = Sleeper()
        self.jitter = 0.0
        self.connects = 0
        self.link = self.make_link()

    def make_link(self) -> BrainLink:
        async def counting(path: Path) -> LinkSocket:
            self.connects += 1
            return await client_mod._unix_connector(path)

        return BrainLink(
            self.sock,
            self.token_file,
            client_version="0.3-test",
            tools=HostToolDispatcher(self.backends),
            on_state=self.states.append,
            connector=counting,
            sleep=self.sleeper,
            jitter=lambda: self.jitter,
        )

    async def up(self) -> None:
        await self.brain.start()
        self.link.start()
        await until(lambda: self.link.state.status == "up")

    def ask(self, text: str = "What does this button do?", **extra: object) -> tuple[str, Answer]:
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

    async def sessions_send(self, message: object) -> None:
        await self.brain.sessions[-1].send(message)

    async def __aenter__(self) -> Rig:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.link.stop()
        await self.brain.stop()

    @property
    def reasons(self) -> list[str]:
        return [s.reason for s in self.states if s.status == "down"]


async def until(condition: Callable[[], object], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.002)


async def settle() -> None:
    """Give both ends a moment to act on anything already sent."""
    await asyncio.sleep(0.05)


@pytest.fixture
def root() -> Iterator[Path]:
    # Not tmp_path: a Unix socket path must fit in 108 bytes.
    path = Path(tempfile.mkdtemp(prefix="wl-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def rig(root: Path) -> Rig:
    return Rig(root)


@pytest.fixture
def logs() -> Iterator[list[str]]:
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    logger = logging.getLogger("waddle")
    handler = Collect()
    level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield lines
    logger.removeHandler(handler)
    logger.setLevel(level)


# ---- handshake ---------------------------------------------------------------------------


async def test_handshake_sends_the_token_and_reports_up_with_models(rig: Rig) -> None:
    async with rig:
        await rig.up()
        assert [s.status for s in rig.states] == ["connecting", "up"]
        assert rig.link.state == LinkState("up", models=fb.MODELS)
        (hello,) = rig.brain.hellos
        assert (hello.token, hello.client_version) == (TOKEN, "0.3-test")


async def test_a_wrong_token_is_refused_and_retried_only_at_the_cap(rig: Rig) -> None:
    async with rig:
        rig.brain.token = "00" * 32
        await rig.brain.start()
        rig.link.start()
        await until(lambda: rig.sleeper.delays)
        assert rig.link.state == LinkState("down", "bad_token")
        assert rig.sleeper.delays == [30.0]
        rig.sleeper.release()
        await until(lambda: len(rig.sleeper.delays) == 2)
        assert rig.sleeper.delays == [30.0, 30.0]
        assert rig.brain.connections == 2
        with pytest.raises(BrainDownError):
            rig.ask()


@pytest.mark.parametrize(
    ("handshake", "reason"),
    [
        ("refuse", "busy"),
        ("garbage", "bad_handshake"),
        ("drop", "connect_failed"),
        ("silent", "handshake_timeout"),
    ],
)
async def test_a_failed_handshake_is_down_with_a_reason(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, handshake: fb.Handshake, reason: str
) -> None:
    monkeypatch.setattr(client_mod, "HANDSHAKE_TIMEOUT", 0.1)
    async with rig:
        rig.brain.handshake = handshake
        await rig.brain.start()
        rig.link.start()
        await until(lambda: rig.sleeper.delays)
        assert rig.link.state == LinkState("down", reason)
        assert rig.sleeper.delays == [0.5]
        assert "up" not in [s.status for s in rig.states]


async def test_a_brain_message_other_than_welcome_fails_the_handshake(rig: Rig) -> None:
    async def first_message_is_an_answer(ws: object) -> None:
        await ws.recv()  # type: ignore[attr-defined]
        await ws.send(AnswerDelta(id="x", text="hi").model_dump_json())  # type: ignore[attr-defined]
        await ws.wait_closed()  # type: ignore[attr-defined]

    async with rig:
        rig.brain._serve = first_message_is_an_answer  # type: ignore[method-assign,assignment]
        await rig.brain.start()
        rig.link.start()
        await until(lambda: rig.sleeper.delays)
        assert rig.link.state == LinkState("down", "bad_handshake")


async def test_a_welcome_sent_as_binary_fails_the_handshake(rig: Rig) -> None:
    async def binary_welcome(ws: object) -> None:
        await ws.recv()  # type: ignore[attr-defined]
        await ws.send(Welcome(id="x", server_version="x").model_dump_json().encode())  # type: ignore[attr-defined]
        await ws.wait_closed()  # type: ignore[attr-defined]

    async with rig:
        rig.brain._serve = binary_welcome  # type: ignore[method-assign,assignment]
        await rig.brain.start()
        rig.link.start()
        await until(lambda: rig.sleeper.delays)
        assert rig.link.state == LinkState("down", "bad_handshake")
        assert "up" not in [s.status for s in rig.states]


# ---- the socket must be ours before it gets the token ------------------------------------


def _wrong_dir_mode(rig: Rig) -> None:
    rig.dir.chmod(0o750)


def _symlink_to_the_real_socket(rig: Rig) -> None:
    real = rig.dir / "real.sock"
    rig.sock.rename(real)
    rig.sock.symlink_to(real)


def _regular_file(rig: Rig) -> None:
    rig.sock.unlink()
    rig.sock.write_text("")


def _someone_elses(rig: Rig) -> None:
    uid = os.getuid()
    client_mod.os.getuid = lambda: uid + 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "spoil", [_wrong_dir_mode, _symlink_to_the_real_socket, _regular_file, _someone_elses]
)
async def test_an_unsafe_socket_is_never_connected_to(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, spoil: Callable[[Rig], None]
) -> None:
    monkeypatch.setattr(os, "getuid", os.getuid)  # restores what _someone_elses replaces
    async with rig:
        await rig.brain.start()
        spoil(rig)
        rig.link.start()
        await until(lambda: rig.sleeper.delays)
        assert rig.link.state == LinkState("down", "unsafe_socket")
        assert [s.status for s in rig.states] == ["down"]  # never even "connecting"
        assert rig.connects == 0
        assert rig.brain.connections == 0


async def test_a_symlinked_runtime_directory_is_unsafe(rig: Rig, root: Path) -> None:
    async with rig:
        await rig.brain.start()
        alias = root / "alias"
        alias.symlink_to(rig.dir)
        link = BrainLink(
            alias / "brain.sock",
            rig.token_file,
            client_version="t",
            tools=HostToolDispatcher(rig.backends),
            on_state=rig.states.append,
            sleep=rig.sleeper,
        )
        link.start()
        await until(lambda: rig.sleeper.delays)
        await link.stop()
        assert rig.reasons == ["unsafe_socket", "stopped"]
        assert rig.brain.connections == 0


async def test_an_untrusted_token_file_means_no_connection(rig: Rig) -> None:
    async with rig:
        rig.token_file.chmod(0o640)
        await rig.brain.start()
        rig.link.start()
        await until(lambda: rig.sleeper.delays)
        assert rig.link.state == LinkState("down", "no_token")
        assert rig.brain.connections == 0


# ---- backoff, kick, reconnect ------------------------------------------------------------


async def test_backoff_doubles_from_half_a_second_to_the_cap(rig: Rig) -> None:
    async with rig:
        rig.sleeper.instant = True
        rig.link.start()  # no brain at all
        await until(lambda: len(rig.sleeper.delays) >= 9)
        assert rig.sleeper.delays[:9] == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
        assert rig.link.state == LinkState("down", "no_socket")
        assert rig.states == [LinkState("down", "no_socket")]  # told once, not per attempt


async def test_jitter_shortens_a_delay_by_up_to_a_quarter(rig: Rig) -> None:
    async with rig:
        rig.sleeper.instant = True
        rig.jitter = 1.0
        rig.link.start()
        await until(lambda: len(rig.sleeper.delays) >= 8)
        assert rig.sleeper.delays[:2] == [0.375, 0.75]
        assert rig.sleeper.delays[7] == 22.5


async def test_kick_retries_now(rig: Rig) -> None:
    async with rig:
        rig.link.start()
        await until(lambda: rig.sleeper.delays)
        await rig.brain.start()
        await settle()
        assert rig.link.state.status == "down"  # the fake sleep never ends by itself
        rig.link.kick()
        await until(lambda: rig.link.state.status == "up")
        assert rig.sleeper.delays == [0.5]


async def test_reconnects_after_the_brain_restarts(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_mod, "STABLE_AFTER", 0.0)  # these short sessions count as up
    async with rig:
        rig.link.start()
        for _ in range(3):  # three failures: the next delay would be 4 s
            await until(lambda: rig.sleeper.delays)
            rig.sleeper.delays.clear()
            rig.sleeper.release()
        await rig.brain.start()
        rig.link.kick()
        await until(lambda: rig.link.state.status == "up")
        rig.sleeper.delays.clear()

        await rig.brain.stop()
        await until(lambda: rig.sleeper.delays)
        assert rig.link.state == LinkState("down", "lost")
        assert rig.sleeper.delays == [0.5]  # it was up, so the schedule starts over

        rig.sleeper.release()
        await until(lambda: len(rig.sleeper.delays) == 2)
        assert rig.link.state == LinkState("down", "no_socket")
        await rig.brain.start()
        rig.sleeper.release()
        await until(lambda: rig.link.state.status == "up")
        assert rig.brain.connections == 2
        _, answer = rig.ask()
        await until(lambda: rig.brain.of_type(Ask))
        assert answer.events == []


async def test_a_brain_that_keeps_dropping_us_is_not_retried_in_a_tight_loop(rig: Rig) -> None:
    async def welcome_then_drop(session: fb.Session) -> None:
        await session.drop()

    async with rig:
        rig.sleeper.instant = True
        rig.brain.on_welcome = welcome_then_drop
        await rig.brain.start()
        rig.link.start()
        await until(lambda: len(rig.sleeper.delays) >= 8)
        assert rig.sleeper.delays[:8] == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        assert rig.brain.connections >= 8  # each of those was a real welcome and a real loss


async def test_a_link_that_stayed_up_restarts_the_schedule(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_mod, "STABLE_AFTER", 0.0)

    async def welcome_then_drop(session: fb.Session) -> None:
        await session.drop()

    async with rig:
        rig.sleeper.instant = True
        rig.brain.on_welcome = welcome_then_drop
        await rig.brain.start()
        rig.link.start()
        await until(lambda: len(rig.sleeper.delays) >= 4)
        assert rig.sleeper.delays[:4] == [0.5, 0.5, 0.5, 0.5]


# ---- asking ------------------------------------------------------------------------------


async def test_ask_sends_images_first_and_streams_the_answer(rig: Rig) -> None:
    async def reply(session: fb.Session, ask: Ask) -> None:
        await session.send(AnswerDelta(id=ask.id, text="It "))
        await session.send(AnswerDelta(id=ask.id, text="saves."))
        await session.send(AnswerDone(id=ask.id, sources=("screen",), role="fast_vlm"))

    async with rig:
        rig.brain.on_ask = reply
        await rig.up()
        app = AppContext(app_id="org.kde.kate", name="Kate", version="24.2")
        window = WindowContext(resource_class="kate", title="notes.txt", width=800, height=600)
        request_id, answer = rig.ask(
            "What is this?",
            app=app,
            window=window,
            images=[("overview", "image/jpeg", JPEG), ("crop", "image/png", PNG)],
            can_see=True,
        )
        await until(lambda: answer.ended)

        first, second, ask = rig.brain.received
        assert isinstance(ask, Ask)
        assert first[0] == second[0] == "image"  # type: ignore[index]
        assert (first[2], first[3]) == ("image/jpeg", JPEG)  # type: ignore[index]
        assert (second[2], second[3]) == ("image/png", PNG)  # type: ignore[index]
        assert [(r.image_id, r.kind) for r in ask.images] == [
            (first[1], "overview"),  # type: ignore[index]
            (second[1], "crop"),  # type: ignore[index]
        ]
        assert (ask.id, ask.text, ask.app, ask.window) == (request_id, "What is this?", app, window)
        assert (ask.can_see, ask.blind_reason) == (True, None)

        assert [e[0] for e in answer.events] == ["delta", "delta", "done"]
        assert [e[1] for e in answer.events[:2]] == ["It ", "saves."]
        done = answer.events[2][1]
        assert isinstance(done, AnswerDone) and done.role == "fast_vlm"

        await rig.sessions_send(AnswerDelta(id=request_id, text="late"))  # after the end
        await settle()
        assert len(answer.events) == 3


async def test_a_blind_ask_carries_the_reason_and_no_images(rig: Rig) -> None:
    async with rig:
        await rig.up()
        rig.ask(blind_reason="paused")
        await until(lambda: rig.brain.received)
        (ask,) = rig.brain.received
        assert isinstance(ask, Ask)
        assert (ask.images, ask.can_see, ask.blind_reason) == ((), False, "paused")


async def test_two_asks_are_routed_by_id(rig: Rig) -> None:
    async with rig:
        await rig.up()
        id_a, a = rig.ask("first")
        id_b, b = rig.ask("second")
        assert id_a != id_b
        await until(lambda: len(rig.brain.received) == 2)
        await rig.sessions_send(AnswerDelta(id=id_b, text="B"))
        await rig.sessions_send(AnswerError(id=id_a, code="busy"))
        await rig.sessions_send(AnswerDone(id=id_b))
        await until(lambda: a.ended and b.ended)
        assert a.events == [("error", "busy")]
        assert [e[0] for e in b.events] == ["delta", "done"]


async def test_ask_raises_when_down_and_calls_nothing(rig: Rig) -> None:
    async with rig:
        with pytest.raises(BrainDownError):
            rig.ask()  # never started
        rig.link.start()
        await until(lambda: rig.sleeper.delays)
        answer = Answer()
        with pytest.raises(BrainDownError):
            rig.link.ask(
                "q", app=None, window=None, can_see=False, blind_reason=None, **answer.kwargs()
            )
        assert answer.events == []


@pytest.mark.parametrize(
    "bad",
    [
        {"text": ""},
        {"text": "x" * 4001},
        {"images": [("overview", "image/jpeg", JPEG)] * 3},
        {"images": [("overview", "image/gif", JPEG)]},
        {"images": [("overview", "image/jpeg", b"")]},
        {
            "images": [
                ("overview", "image/png", PNG),
                ("overview", "image/jpeg", b"x" * (8 << 20) + b"x"),
            ]
        },
    ],
)
async def test_an_ask_that_cannot_go_on_the_wire_sends_nothing(
    rig: Rig, bad: dict[str, object]
) -> None:
    async with rig:
        await rig.up()
        with pytest.raises(ValueError):
            rig.ask(**{"text": "fine", **bad})  # type: ignore[arg-type]
        await settle()
        assert rig.brain.received == []
        assert rig.link._requests == {}


async def test_cancel_ends_the_request_once_and_tells_the_brain(rig: Rig, logs: list[str]) -> None:
    async with rig:
        await rig.up()
        request_id, answer = rig.ask()
        await rig.sessions_send(AnswerDelta(id=request_id, text="So"))
        await until(lambda: answer.events)
        rig.link.cancel(request_id)
        assert answer.events == [("delta", "So"), ("error", "cancelled")]
        rig.link.cancel(request_id)  # twice is harmless
        rig.link.cancel("never-existed")
        await until(lambda: rig.brain.of_type(Cancel))
        assert [c.id for c in rig.brain.of_type(Cancel)] == [request_id]

        # The brain was still talking, then confirms: none of it reaches the caller.
        await rig.sessions_send(AnswerDelta(id=request_id, text="late"))
        await rig.sessions_send(AnswerError(id=request_id, code="cancelled"))
        await settle()
        assert len(answer.events) == 2
        assert not [line for line in logs if "link_unknown_id" in line]
        assert rig.link.state.status == "up"


async def test_prewarm_is_sent_when_up_and_dropped_when_down(rig: Rig) -> None:
    async with rig:
        rig.link.prewarm("smart_model")  # down: nothing happens
        await rig.up()
        rig.link.prewarm("fast_vlm")
        await until(lambda: rig.brain.received)
        assert [p.role for p in rig.brain.of_type(Prewarm)] == ["fast_vlm"]


async def test_state_messages_update_the_link_state(rig: Rig) -> None:
    async with rig:
        await rig.up()
        await rig.sessions_send(State(id="s1", models=fb.MODELS, waking="smart_model"))
        await until(lambda: rig.link.state.waking)
        assert rig.states[-1] == LinkState("up", models=fb.MODELS, waking="smart_model")
        count = len(rig.states)
        await rig.sessions_send(State(id="s2", models=fb.MODELS, waking="smart_model"))
        await rig.sessions_send(State(id="s3"))
        await until(lambda: rig.link.state.waking is None)
        assert rig.states[count:] == [LinkState("up")]  # the unchanged one was not repeated


# ---- a brain that dies or misbehaves -----------------------------------------------------


async def test_brain_dying_mid_answer_ends_every_request_once(rig: Rig) -> None:
    async with rig:
        await rig.up()
        id_a, a = rig.ask("first")
        _, b = rig.ask("second")
        await rig.sessions_send(AnswerDelta(id=id_a, text="Half an ans"))
        await until(lambda: a.events)
        await rig.brain.stop()
        await until(lambda: a.ended and b.ended)
        await until(lambda: rig.sleeper.delays)
        assert a.events == [("delta", "Half an ans"), ("error", "brain_lost")]
        assert b.events == [("error", "brain_lost")]
        assert rig.link.state == LinkState("down", "lost")
        with pytest.raises(BrainDownError):
            rig.ask()
        rig.link.cancel(id_a)  # the UI may still try: nothing more happens
        assert len(a.events) == 2


@pytest.mark.parametrize(
    "garbage",
    [
        "{not json",
        '{"v":1,"id":"x","type":"answer.delta","text":"hi","extra":1}',
        '{"v":2,"id":"x","type":"pong"}',
        '{"v":1,"id":"x","type":"ask","text":"a host message"}',
        Welcome(id="again", server_version="x").model_dump_json(),
        b"\x00\x00\x00\x02{}binary",
        "x" * (256 * 1024 + 1),
        AnswerDelta(id="x", text="well-formed, but sent as binary").model_dump_json().encode(),
    ],
)
async def test_a_protocol_violation_drops_the_brain_and_backs_off_at_the_cap(
    rig: Rig, logs: list[str], garbage: str | bytes
) -> None:
    async with rig:
        await rig.up()
        _, answer = rig.ask()
        await until(lambda: rig.brain.received)
        await rig.brain.sessions[0].send_raw(garbage)
        await until(lambda: rig.sleeper.delays)
        assert answer.events == [("error", "brain_lost")]
        assert rig.link.state == LinkState("down", "protocol")
        assert rig.sleeper.delays == [30.0]
        assert len([line for line in logs if "link_protocol_violation" in line]) == 1


async def test_messages_for_unknown_ids_are_ignored_and_logged(rig: Rig, logs: list[str]) -> None:
    async with rig:
        await rig.up()
        request_id, answer = rig.ask()
        await rig.sessions_send(AnswerDelta(id="forged", text=CANARY))
        await rig.sessions_send(AnswerDone(id="forged"))
        await rig.sessions_send(AnswerError(id="forged", code="internal"))
        await rig.sessions_send(AnswerDelta(id=request_id, text="real"))
        await until(lambda: answer.events)
        assert answer.events == [("delta", "real")]
        assert rig.link.state.status == "up"
        unknown = [line for line in logs if "link_unknown_id" in line]
        assert len(unknown) == 3
        assert "forged" not in "".join(logs)


async def test_a_flood_of_stray_messages_drops_the_brain(rig: Rig, logs: list[str]) -> None:
    async with rig:
        await rig.up()
        _, answer = rig.ask()
        await until(lambda: rig.brain.received)
        session = rig.brain.sessions[0]
        with contextlib.suppress(Exception):  # the host hangs up part-way through
            for n in range(client_mod.MAX_STRAYS * 4):
                if n % 2:
                    await session.send(AnswerDelta(id="forged", text="x"))
                else:
                    await session.send(ToolCall(id="forged", call_id="c", tool="get_app_state"))
        await until(lambda: rig.sleeper.delays)
        assert rig.link.state == LinkState("down", "protocol")
        assert rig.sleeper.delays == [30.0]
        assert answer.events == [("error", "brain_lost")]
        strays = [line for line in logs if "link_unknown_id" in line or "tool_rejected" in line]
        assert len(strays) <= client_mod.MAX_STRAYS + 1
        assert len(rig.brain.of_type(ToolResult)) <= client_mod.MAX_STRAYS


async def test_strays_are_counted_per_question_not_for_ever(rig: Rig) -> None:
    async with rig:
        await rig.up()
        for _ in range(3):
            request_id, answer = rig.ask()
            for _ in range(client_mod.MAX_STRAYS - 1):
                await rig.sessions_send(AnswerDelta(id="forged", text="x"))
            await rig.sessions_send(AnswerDone(id=request_id))
            await until(lambda answer=answer: answer.ended)
        assert rig.link.state.status == "up"


async def test_a_protocol_violation_ends_requests_before_the_close_handshake(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brain that misbehaves and then will not finish closing must not hold the UI up."""
    monkeypatch.setattr(client_mod, "CLOSE_TIMEOUT", 0.3)
    closing = asyncio.Event()
    codes: list[int] = []

    class Stuck:
        def __init__(self) -> None:
            self.replies: asyncio.Queue[str] = asyncio.Queue()

        async def send(self, message: str | bytes) -> None:
            assert isinstance(message, str)
            if '"hello"' in message:
                self.replies.put_nowait(Welcome(id="w", server_version="x").model_dump_json())
            elif '"ask"' in message:
                self.replies.put_nowait("{not json")

        async def recv(self) -> str | bytes:
            return await self.replies.get()

        async def close(self, code: int = 1000) -> None:
            codes.append(code)
            closing.set()
            await asyncio.Event().wait()  # the other end never answers the close

    async def connector(path: Path) -> LinkSocket:
        return Stuck()

    link = BrainLink(
        rig.sock,
        rig.token_file,
        client_version="t",
        tools=HostToolDispatcher(rig.backends),
        on_state=rig.states.append,
        connector=connector,
        sleep=rig.sleeper,
    )
    await rig.brain.start()  # only so that the socket path passes the checks
    try:
        link.start()
        await until(lambda: link.state.status == "up")
        answer = Answer()
        link.ask("q", app=None, window=None, can_see=False, blind_reason=None, **answer.kwargs())
        await until(closing.is_set)
        assert answer.events == [("error", "brain_lost")]  # already, while close is stuck
        assert link.state == LinkState("down", "protocol")
        assert rig.sleeper.delays == []
        assert codes == [1008]  # policy violation
        await until(lambda: rig.sleeper.delays)  # and the stuck close is given up on
        with pytest.raises(BrainDownError):
            link.ask(
                "q", app=None, window=None, can_see=False, blind_reason=None, **answer.kwargs()
            )
    finally:
        await link.stop()
        await rig.brain.stop()


async def test_a_failing_callback_does_not_break_the_link(rig: Rig) -> None:
    def boom(_: object) -> None:
        raise RuntimeError("ui bug")

    async with rig:
        await rig.up()
        request_id, answer = rig.ask(on_delta=boom)
        await rig.sessions_send(AnswerDelta(id=request_id, text="x"))
        await rig.sessions_send(AnswerDone(id=request_id))
        await until(lambda: answer.ended)
        assert rig.link.state.status == "up"


async def test_stop_ends_open_requests_and_can_start_again(rig: Rig) -> None:
    async with rig:
        await rig.up()
        _, answer = rig.ask()
        await rig.link.stop()
        assert answer.events == [("error", "brain_lost")]
        assert rig.link.state == LinkState("down", "stopped")
        rig.link.start()
        await until(lambda: rig.link.state.status == "up")


# ---- tool calls --------------------------------------------------------------------------


async def test_a_tool_call_for_an_open_request_is_run_and_answered(rig: Rig) -> None:
    async with rig:
        await rig.up()
        request_id, answer = rig.ask()
        await rig.sessions_send(
            ToolCall(id=request_id, call_id="c1", tool="man", args={"name": "ls", "section": "1"})
        )
        await until(lambda: rig.brain.of_type(ToolResult))
        (result,) = rig.brain.of_type(ToolResult)
        assert (result.id, result.call_id, result.ok) == (request_id, "c1", True)
        assert result.data == {"ok": "man"}
        assert rig.backends.calls == [("man", "ls", "1")]
        assert answer.events == []  # a tool call is not an answer


@pytest.mark.parametrize("when", ["never_asked", "after_done", "after_cancel"])
async def test_a_tool_call_without_an_open_request_is_refused(rig: Rig, when: str) -> None:
    async with rig:
        await rig.up()
        request_id = "forged"
        if when != "never_asked":
            request_id, answer = rig.ask()
            if when == "after_cancel":
                rig.link.cancel(request_id)
            else:
                await rig.sessions_send(AnswerDone(id=request_id))
                await until(lambda: answer.ended)
        await rig.sessions_send(ToolCall(id=request_id, call_id="c1", tool="get_app_state"))
        await until(lambda: rig.brain.of_type(ToolResult))
        (result,) = rig.brain.of_type(ToolResult)
        assert (result.ok, result.error, result.data) == (False, "no_open_request", None)
        assert rig.backends.calls == []


async def test_tool_calls_are_capped_per_request(rig: Rig) -> None:
    async with rig:
        await rig.up()
        rig.backends.gate = asyncio.Event()  # all still running: the cap counts started calls
        request_id, _ = rig.ask()
        other_id, _ = rig.ask("another")
        for n in range(MAX_TOOL_CALLS_PER_REQUEST + 2):
            await rig.sessions_send(ToolCall(id=request_id, call_id=f"c{n}", tool="get_app_state"))
        await until(lambda: len(rig.brain.of_type(ToolResult)) == 2)
        assert {r.error for r in rig.brain.of_type(ToolResult)} == {"cap_reached"}
        await rig.sessions_send(
            ToolCall(id=other_id, call_id="o1", tool="tldr", args={"name": "x"})
        )
        rig.backends.gate.set()
        await until(lambda: len(rig.brain.of_type(ToolResult)) == MAX_TOOL_CALLS_PER_REQUEST + 3)
        results = {r.call_id: r for r in rig.brain.of_type(ToolResult)}
        assert [results[f"c{n}"].ok for n in range(6)] == [True] * 4 + [False] * 2
        assert results["o1"].ok  # the cap is per request
        assert len(rig.backends.calls) == MAX_TOOL_CALLS_PER_REQUEST + 1


async def test_bad_tool_calls_are_refused_but_still_count(rig: Rig) -> None:
    async with rig:
        await rig.up()
        request_id, _ = rig.ask()
        bad = [
            ("run_shell", {}),
            ("man", {"name": "--pager=sh"}),
            ("tldr", {}),
            ("man", {"name": 1}),
        ]
        for n, (tool, args) in enumerate(bad):
            await rig.sessions_send(ToolCall(id=request_id, call_id=f"c{n}", tool=tool, args=args))
        await rig.sessions_send(ToolCall(id=request_id, call_id="c4", tool="get_app_state"))
        await until(lambda: len(rig.brain.of_type(ToolResult)) == 5)
        errors = {r.call_id: r.error for r in rig.brain.of_type(ToolResult)}
        assert errors == {
            "c0": "unknown_tool",
            "c1": "bad_arguments",
            "c2": "bad_arguments",
            "c3": "bad_arguments",
            "c4": "cap_reached",
        }
        assert rig.backends.calls == []


async def test_a_slow_tool_does_not_hold_up_the_answer_and_dies_with_the_link(rig: Rig) -> None:
    async with rig:
        await rig.up()
        rig.backends.gate = asyncio.Event()
        request_id, answer = rig.ask()
        await rig.sessions_send(ToolCall(id=request_id, call_id="c1", tool="get_app_state"))
        await rig.sessions_send(AnswerDelta(id=request_id, text="meanwhile"))
        await until(lambda: answer.events)
        assert rig.backends.calls == [("app_state",)]
        await rig.brain.stop()
        await until(lambda: answer.ended)
        await until(lambda: not rig.link._tool_tasks)


# ---- nothing private in the logs ---------------------------------------------------------


async def test_canaries_never_reach_the_logs(rig: Rig, logs: list[str]) -> None:
    async with rig:
        await rig.up()
        request_id, answer = rig.ask(
            f"wording {CANARY}",
            app=AppContext(app_id=CANARY, name=CANARY),
            window=WindowContext(resource_class=CANARY, title=f"wording {CANARY}"),
            images=[("overview", "image/png", CANARY.encode() * 10)],
            can_see=True,
        )
        await rig.sessions_send(
            ToolCall(id=request_id, call_id="c1", tool="man", args={"name": CANARY})
        )
        await rig.sessions_send(
            ToolCall(id=request_id, call_id="c2", tool=CANARY, args={"x": CANARY})
        )
        await rig.sessions_send(
            ToolCall(id=CANARY, call_id=CANARY, tool="tldr", args={"name": CANARY})
        )
        await rig.sessions_send(AnswerDelta(id=request_id, text=f"wording {CANARY}"))
        await rig.sessions_send(AnswerDelta(id=CANARY, text=CANARY))
        await until(lambda: len(rig.brain.of_type(ToolResult)) == 3 and answer.events)
        await rig.brain.sessions[0].send_raw('{"v":1,"id":"x","type":"' + CANARY + '"}')
        await until(lambda: answer.ended)
        await rig.link.stop()

        text = "\n".join(logs)
        assert len(logs) > 8  # the handler really is attached
        for secret in (CANARY, TOKEN, request_id, "wording", str(rig.sock), str(rig.token_file)):
            assert secret not in text
