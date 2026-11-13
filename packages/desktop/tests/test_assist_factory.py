"""``build_assist``: the pieces wired together, against a fake brain on a real socket."""

from __future__ import annotations

import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import assist_helpers as h
import fake_brain as fb

from waddle_core import capabilities as caps
from waddle_core.capabilities import degradations
from waddle_core.link import AnswerDelta, AnswerDone, Ask, ToolCall, ToolResult
from waddle_core.paths import WaddlePaths
from waddle_desktop.assist import Assist, build_assist
from waddle_desktop.assist.shared_exec import SharedExec
from waddle_desktop.identity.cache import IdentityCache
from waddle_desktop.link.client import LinkState
from waddle_desktop.link.token import TokenError, read_token

CANARY = "CANARY-9e41"


class Rig:
    def __init__(self, root: Path) -> None:
        self.paths = WaddlePaths(
            config_dir=root / "config",
            data_dir=root / "data",
            cache_dir=root / "cache",
            runtime_dir=root / "run",
        )
        self.paths.runtime_dir.mkdir(mode=0o700)
        self.eyes = h.SensesRig()
        self.fake = h.FakeExec()
        self.events: list[tuple[str, object]] = []
        self.states: list[LinkState] = []
        self.identity_changes = 0
        self.brain: fb.FakeBrain | None = None

    def build(self, **options: object) -> Assist:
        self.assist = build_assist(
            self.paths,
            self.eyes.senses,
            client_version="0.3-test",
            cache=IdentityCache(self.paths.identity_cache),
            on_delta=lambda text: self.events.append(("delta", text)),
            on_done=lambda sources, role: self.events.append(("done", (sources, role))),
            on_error=lambda code: self.events.append(("error", code)),
            on_link_state=self.states.append,
            on_identity=self._identity_changed,
            packs=h.fixture_packs(),
            sandbox=SharedExec(lambda: self.fake),
            **options,  # type: ignore[arg-type]
        )
        return self.assist

    def _identity_changed(self) -> None:
        self.identity_changes += 1

    async def up(self) -> fb.FakeBrain:
        token = read_token(self.paths.link_token)
        assert token is not None
        self.brain = fb.FakeBrain(self.paths.brain_socket, token)
        await self.brain.start()
        self.assist.start()
        await h.until(lambda: self.assist.link.state.status == "up")
        return self.brain

    async def close(self) -> None:
        await self.assist.stop()
        if self.brain is not None:
            await self.brain.stop()
        await self.eyes.senses.stop()


@pytest.fixture
def root() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="wf-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def test_the_token_is_made_before_anything_connects(root: Path) -> None:
    rig = Rig(root)
    assert not rig.paths.link_token.exists()
    assist = rig.build()
    token = read_token(rig.paths.link_token)
    assert token is not None and len(token) == 64 and assist.token_error is None
    assert stat.S_IMODE(rig.paths.link_token.stat().st_mode) == 0o600
    assert assist.link.state.status == "down"  # built, not started
    brain = await rig.up()
    assert [hello.token for hello in brain.hellos] == [token]
    await rig.close()


async def test_a_token_that_cannot_be_made_does_not_stop_waddle(root: Path) -> None:
    rig = Rig(root)

    def broken(_path: Path) -> str:
        raise TokenError("token_unwritable")

    with h.captured_logs() as logs:
        assist = rig.build(make_token=broken)
    assert assist.token_error == "token_unwritable"
    assert any("link_token_failed" in line for line in logs)
    assist.start()
    await h.until(lambda: assist.link.state.status == "down" and assist.link.state.reason)
    await rig.close()


async def test_a_question_goes_out_with_identity_and_the_brain_can_use_the_tools(
    root: Path,
) -> None:
    rig = Rig(root)
    rig.build()
    rig.paths.capture_bin.parent.mkdir(parents=True)
    rig.paths.capture_bin.write_bytes(b"\x7fELF")  # "installed"; the fake eye stands in for it
    brain = await rig.up()
    await rig.eyes.focus(h.win(caption="scene - Good 5.3"))
    await h.until(lambda: rig.assist.identity.settled)
    assert rig.identity_changes == 1

    async def on_ask(session: fb.Session, ask: Ask) -> None:
        await session.send(ToolCall(id=ask.id, call_id="c1", tool="get_app_state"))
        await session.send(ToolCall(id=ask.id, call_id="c2", tool="man", args={"name": "ls"}))

    brain.on_ask = on_ask
    rig.fake.results = [h.result("LS(1) list directory contents")]
    await rig.assist.asker.ask("what am I looking at?")
    await h.until(lambda: len(brain.of_type(ToolResult)) == 2)
    ask = brain.of_type(Ask)[0]
    assert ask.app is not None and ask.window is not None
    assert (ask.app.name, ask.app.version, ask.can_see) == ("Good App", "5.3.1", True)
    assert ask.window.title == "scene - Good 5.3" and len(ask.images) == 2
    results = {r.call_id: r for r in brain.of_type(ToolResult)}
    assert results["c1"].ok and results["c1"].data == {
        "can_see": True,
        "app": "Good App",
        "version": "5.3.1",
        "packaging": "deb",
        "channel": "stable",
        "window_class": "GoodApp",
        "window_title": "scene - Good 5.3",
    }
    assert results["c2"].ok and results["c2"].data == {
        "page": "ls",
        "text": "LS(1) list directory contents",
        "truncated": False,
    }
    await brain.sessions[-1].send(AnswerDelta(id=ask.id, text="You are in Good App 5.3."))
    await brain.sessions[-1].send(AnswerDone(id=ask.id, sources=("screen", "app")))
    await h.until(lambda: len(rig.events) == 2)
    assert rig.events == [
        ("delta", "You are in Good App 5.3."),
        ("done", (("screen", "app"), None)),
    ]
    assert [s.status for s in rig.states] == ["connecting", "up"]
    await rig.close()


async def test_a_blocked_window_leaks_nothing_through_the_ask_or_the_tools(root: Path) -> None:
    rig = Rig(root)
    rig.build()
    brain = await rig.up()
    await rig.eyes.focus(h.win(cls="KeePassXC", caption=f"{CANARY} - KeePassXC"))

    async def on_ask(session: fb.Session, ask: Ask) -> None:
        await session.send(ToolCall(id=ask.id, call_id="c1", tool="get_app_state"))

    brain.on_ask = on_ask
    with h.captured_logs() as logs:
        await rig.assist.asker.ask("what is this?")
        await h.until(lambda: brain.of_type(ToolResult))
    (result,) = brain.of_type(ToolResult)
    assert result.data == {"can_see": False, "reason": "blocked"}
    assert not [m for m in brain.received if isinstance(m, tuple)]  # no image was sent
    sent = "".join(m.model_dump_json() for m in brain.received if not isinstance(m, tuple))
    assert CANARY not in sent
    assert "KeePassXC" not in sent
    assert CANARY not in "".join(logs) and "KeePassXC" not in "".join(logs)
    await rig.close()


async def test_stopping_ends_the_open_question_and_the_link(root: Path) -> None:
    rig = Rig(root)
    rig.build()
    brain = await rig.up()
    await rig.eyes.focus(h.win())
    await rig.assist.asker.ask("still there?")
    await h.until(lambda: brain.of_type(Ask))
    await rig.assist.stop()
    assert rig.events == [("error", "cancelled")]
    assert rig.assist.link.state == LinkState("down", "stopped")
    await rig.close()


async def test_capabilities_follow_the_link_the_helper_and_the_sandbox(root: Path) -> None:
    rig = Rig(root)
    assist = rig.build()
    before = assist.capabilities()
    assert (before.brain, before.vision, before.capture) == ("down", False, False)
    assert before.capture_reason == caps.CAPTURE_HELPER_MISSING
    assert (before.sandbox, before.sandbox_reason) == (False, caps.SANDBOX_UNCHECKED)
    assert before.packs == len(assist.packs.packs) > 0
    assert before.pack_errors == len(assist.packs.errors) > 0
    assert "Answers" in [d.what for d in degradations(before)]

    rig.paths.capture_bin.parent.mkdir(parents=True)
    rig.paths.capture_bin.write_bytes(b"\x7fELF")
    assert await assist.check_sandbox() is True
    await rig.up()
    after = assist.capabilities()
    assert (after.brain, after.brain_reason, after.vision) == ("up", "", True)
    assert (after.capture, after.sandbox) == (True, True)
    assert after.can_answer and after.can_see_screen
    assert "Answers" not in [d.what for d in degradations(after)]

    # The helper is there but will not start: found out on the first look.
    rig.eyes.unavailable = "spawn_failed"
    await rig.eyes.focus(h.win())
    await rig.eyes.senses.look_once()
    failed = assist.capabilities()
    assert (failed.capture, failed.capture_reason) == (False, caps.CAPTURE_HELPER_FAILED)
    await rig.close()
    assert assist.capabilities().brain == "down"
