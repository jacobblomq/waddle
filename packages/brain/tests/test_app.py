"""The brain's entry point: config and token checks, a real run to SIGTERM, clean shutdown."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from websockets.sync.client import unix_connect

from waddle_brain import app
from waddle_brain.config import BrainConfig
from waddle_brain.contracts import (
    AnswerResult,
    AnswerSink,
    AskContext,
    HostTools,
    ModelProvider,
)
from waddle_core.link import Hello, ModelStatus, Role, Welcome, decode_brain, encode

TOKEN = "0123456789abcdef" * 4


class FakeProvider:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class FakeAnswerer:
    async def answer(self, ask: AskContext, tools: HostTools, sink: AnswerSink) -> AnswerResult:
        return AnswerResult()

    async def prewarm(self, role: Role) -> None:
        return None

    async def status(self) -> list[ModelStatus]:
        return []


class Fakes:
    def __init__(self) -> None:
        self.provider = FakeProvider()
        self.built: list[str] = []


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    """Stand-ins for the model layer and the answerer, injected where ``main`` imports them."""
    made = Fakes()

    def build_provider(config: BrainConfig) -> FakeProvider:
        made.built.append("provider")
        return made.provider

    def build_answerer(config: BrainConfig, provider: ModelProvider) -> FakeAnswerer:
        assert provider is made.provider
        made.built.append("answerer")
        return FakeAnswerer()

    models = types.ModuleType("waddle_brain.models")
    models.build_provider = build_provider  # type: ignore[attr-defined]
    qa = types.ModuleType("waddle_brain.qa")
    qa.build_answerer = build_answerer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "waddle_brain.models", models)
    monkeypatch.setitem(sys.modules, "waddle_brain.qa", qa)
    return made


def write_config(tmp_path: Path, token: str | None = TOKEN) -> Path:
    if token is not None:
        (tmp_path / "token").write_text(token + "\n")
    config = tmp_path / "brain.toml"
    config.write_text(
        f'socket_path = "{tmp_path}/brain.sock"\n'
        f'token_path = "{tmp_path}/token"\n'
        '[roles.smart_model]\nmodel = "some-model"\n'
    )
    return config


def test_read_token_accepts_only_64_lowercase_hex(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    assert app.read_token(path) == TOKEN
    for bad in ("", "abc", TOKEN.upper(), TOKEN + "0", TOKEN[:-1] + "g", TOKEN + "\n" + TOKEN):
        path.write_text(bad)
        with pytest.raises(app.TokenError) as caught:
            app.read_token(path)
        assert caught.value.code == "token_malformed"
        assert TOKEN[:16] not in str(caught.value)
    path.write_bytes(b"\xff" * 64)
    with pytest.raises(app.TokenError):
        app.read_token(path)
    with pytest.raises(app.TokenError) as caught:
        app.read_token(tmp_path / "missing")
    assert caught.value.code == "token_unreadable"


def test_missing_or_broken_config_exits_2(
    tmp_path: Path, fakes: Fakes, capfd: pytest.CaptureFixture[str]
) -> None:
    assert app.main(["--config", str(tmp_path / "missing.toml")]) == 2
    broken = tmp_path / "broken.toml"
    broken.write_text("roles = 3\n")
    assert app.main(["--config", str(broken)]) == 2
    assert fakes.built == []
    assert '"code":"config_invalid"' in capfd.readouterr().err


@pytest.mark.parametrize("token", [None, "tooshort", TOKEN.upper()])
def test_bad_token_exits_2_without_printing_it(
    tmp_path: Path, fakes: Fakes, capfd: pytest.CaptureFixture[str], token: str | None
) -> None:
    assert app.main(["--config", str(write_config(tmp_path, token))]) == 2
    assert fakes.built == []
    assert not (tmp_path / "brain.sock").exists()
    captured = capfd.readouterr()
    assert '"event":"brain_start_failed"' in captured.err
    if token is not None:
        assert token not in captured.out + captured.err


def test_socket_problem_exits_3_and_closes_the_provider(tmp_path: Path, fakes: Fakes) -> None:
    config = write_config(tmp_path)
    (tmp_path / "brain.sock").write_text("not a socket")
    assert app.main(["--config", str(config)]) == 3
    assert fakes.provider.closed == 1
    assert (tmp_path / "brain.sock").read_text() == "not a socket"


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_serves_until_signalled_then_cleans_up(
    tmp_path: Path,
    fakes: Fakes,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    signum: signal.Signals,
) -> None:
    monkeypatch.setenv(app.CONFIG_ENV, str(write_config(tmp_path)))
    socket_path = tmp_path / "brain.sock"
    seen: list[object] = []

    def host() -> None:
        try:
            deadline = time.monotonic() + 5
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            with unix_connect(str(socket_path)) as ws:
                ws.send(encode(Hello(id="h1", token=TOKEN, client_version="0.1")))
                seen.append(decode_brain(ws.recv(timeout=5)))
        finally:
            os.kill(os.getpid(), signum)

    thread = threading.Thread(target=host)
    thread.start()
    try:
        code = app.main([])  # config path comes from the environment
    finally:
        thread.join()
    assert code == 0
    (welcome,) = seen
    assert isinstance(welcome, Welcome)
    assert fakes.built == ["provider", "answerer"]
    assert fakes.provider.closed == 1
    assert not socket_path.exists()
    captured = capfd.readouterr()
    assert '"event":"brain_stopped"' in captured.err
    assert TOKEN not in captured.out + captured.err


def test_the_provider_is_closed_even_if_stopping_the_server_fails(
    tmp_path: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path)
    (tmp_path / "brain.sock").write_text("not a socket")  # start fails at once, then stop runs

    async def broken_stop(self: object) -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(app.LinkServer, "stop", broken_stop)
    with pytest.raises(RuntimeError, match="stop failed"):
        app.main(["--config", str(config)])
    assert fakes.provider.closed == 1
