"""Asking from the UI: the answer in the bubble, the brain's state on the penguin, versions.

Most tests drive ``WaddleApp`` with a scripted stand-in for the assist layer and an I/O
"thread" that runs things at once, so every step is in order. Two tests use the real I/O
thread: one for where signals land, one end to end against a fake brain on a real socket.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject
from PySide6.QtQml import QQmlExpression, qmlContext
from PySide6.QtQuick import QQuickItem

sys.path.insert(0, str(Path(__file__).parent))
import assist_helpers as h
import fake_brain as fb

from waddle_core.config import WaddleConfig
from waddle_core.link import AnswerDelta, AnswerDone, AppContext, Ask
from waddle_core.mood import Mood
from waddle_core.paths import WaddlePaths
from waddle_core.privacy import BlindReason
from waddle_desktop.assist import Current, build_assist
from waddle_desktop.assist.identity_service import NOTHING
from waddle_desktop.assist.shared_exec import SharedExec
from waddle_desktop.io_thread import IoThread, error_site
from waddle_desktop.link.client import LinkState
from waddle_desktop.link.token import ensure_token
from waddle_desktop.sensors.senses import EyesState
from waddle_desktop.theme.palette import FALLBACK_LIGHT
from waddle_desktop.ui import app as app_module
from waddle_desktop.ui import bridge as bridge_module
from waddle_desktop.ui.app import (
    ASLEEP,
    NO_ANSWER,
    START_FAILED,
    STARTING,
    THINKING,
    VERSION_BUTTON,
    VERSION_MOVED,
    VERSION_PAUSED,
    VERSION_UNREADABLE,
    VersionAsk,
    WaddleApp,
    asleep_text,
    error_text,
    source_line,
    version_ask,
    version_hint,
)
from waddle_desktop.ui.bridge import MAX_BUBBLE_CHARS, Bridge
from waddle_desktop.ui.theme_object import ThemeObject
from waddle_desktop.ui.tray import TrayState

CANARY = (
    'CANARY-7c1e <b>bold</b> <a href="https://example.invalid/x">click</a> ![i](http://x/y.png)'
)
UP = LinkState("up")
DOWN = LinkState("down", "no_socket")


def is_plain(text_item: QObject | None) -> bool:
    """The QML Text item renders its text as plain text (PySide cannot read the enum)."""
    assert text_item is not None
    expression = QQmlExpression(qmlContext(text_item), text_item, "textFormat === Text.PlainText")
    value, undefined = expression.evaluate()  # type: ignore[misc]
    assert not expression.hasError() and not undefined, expression.error().toString()
    return value is True


BLENDER = Current(
    AppContext(app_id="org.blender.Blender", name="Blender"), None, True, ("Help", "About Blender")
)


# ---- stand-ins ---------------------------------------------------------------------------


class InlineIo:
    """Stands in for ``IoThread``: runs everything at once, on the calling thread."""

    running = False  # nothing to join at shutdown

    def call(self, fn: Callable[..., object], *args: object) -> None:
        fn(*args)

    def submit(self, coro: Coroutine[Any, Any, object]) -> None:
        asyncio.run(coro)  # type: ignore[arg-type]

    def stop(self) -> None:
        pass


class FakeAsker:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.cancels = 0
        self.prewarms = 0
        self.script: Callable[[str], None] | None = None  # runs "on the loop" for each ask

    async def ask(self, text: str) -> None:
        self.asked.append(text)
        if self.script is not None:
            self.script(text)

    def cancel(self) -> None:
        self.cancels += 1

    def prewarm(self) -> None:
        self.prewarms += 1


class FakeIdentity:
    def __init__(self) -> None:
        self.now: Current = NOTHING
        self.told: list[str] = []
        self.accepts = True
        self.on_change: Callable[[], None] = lambda: None

    def current(self) -> Current:
        return self.now

    def apply_user_version(self, text: str) -> bool:
        self.told.append(text)
        if not self.accepts:
            return False
        app = self.now.app
        assert app is not None
        self.now = Current(app.model_copy(update={"version": "5.3"}), None, False, ())
        self.on_change()
        return True


class FakeAssist:
    def __init__(self) -> None:
        self.asker = FakeAsker()
        self.identity = FakeIdentity()


def short_paths(root: Path) -> WaddlePaths:
    paths = WaddlePaths(
        config_dir=root / "c", data_dir=root / "d", cache_dir=root / "k", runtime_dir=root / "r"
    )
    for directory in paths.all_dirs():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return paths


@pytest.fixture
def root() -> Iterator[Path]:
    # A Unix socket path must be short; pytest's tmp_path is not.
    path = Path(tempfile.mkdtemp(prefix="wu-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


class Rig:
    """A ``WaddleApp`` whose assist layer is scripted and whose I/O runs inline."""

    def __init__(self, paths: WaddlePaths) -> None:
        self.app = WaddleApp(WaddleConfig(), paths)
        self.app._io = InlineIo()  # type: ignore[assignment]
        self.assist = FakeAssist()
        self.app._assist = self.assist  # type: ignore[assignment]
        self.app._relay.assist = self.assist  # type: ignore[assignment]
        self.relay = self.app._relay
        self.assist.identity.on_change = self.relay.identity_changed
        self.bridge = self.app._bridge
        self.app._view.show()  # items of a window that is not shown all read as invisible
        self.app._senses.session_changed(h.UNLOCKED)  # as on a desktop that is not locked
        self.app._on_link_state(UP)

    def ask(self, text: str = "what is this?") -> None:
        self.bridge.submitText(text)

    def flush(self) -> None:
        self.bridge._flush_stream()

    @property
    def text(self) -> str:
        return str(self.bridge.property("bubbleText"))

    @property
    def note(self) -> str:
        return str(self.bridge.property("bubbleNote"))

    @property
    def action(self) -> str:
        return str(self.bridge.property("bubbleAction"))

    @property
    def busy(self) -> bool:
        return bool(self.bridge.property("bubbleBusy"))

    @property
    def mood(self) -> Mood:
        return self.bridge.current_mood

    def item(self, name: str) -> QObject:
        scene = self.app._view.rootObject()
        assert scene is not None
        found = scene.findChild(QObject, name)
        assert found is not None, name
        return found


@pytest.fixture
def rig(qtbot: Any, root: Path) -> Iterator[Rig]:
    made = Rig(short_paths(root))
    yield made
    made.app.shutdown()


@pytest.fixture
def bridge(qtbot: Any) -> Bridge:
    return Bridge(ThemeObject(FALLBACK_LIGHT))


# ---- the bridge: one streamed answer -----------------------------------------------------


def test_a_fast_stream_is_shown_in_a_few_updates_not_one_per_piece(
    bridge: Bridge, qtbot: Any
) -> None:
    bridge.begin_stream("Thinking…")
    updates: list[str] = []
    bridge.bubbleChanged.connect(lambda: updates.append(str(bridge.property("bubbleText"))))
    for index in range(200):
        bridge.push_delta(f"w{index} ")
    assert updates == [], "nothing is laid out until the timer fires"
    assert bridge.property("bubbleText") == "Thinking…"
    whole = "".join(f"w{index} " for index in range(200))
    qtbot.waitUntil(lambda: bridge.property("bubbleText") == whole, timeout=2000)
    assert updates == [whole], "200 pieces, one update"
    bridge.push_delta("more")
    qtbot.waitUntil(lambda: bridge.property("bubbleText") == whole + "more", timeout=2000)
    assert bridge.property("bubbleBusy") is True


def test_the_bubble_never_holds_more_than_its_cap(bridge: Bridge) -> None:
    bridge.begin_stream()
    for _ in range(30):
        bridge.push_delta("x" * 1000)
    bridge._flush_stream()
    text = str(bridge.property("bubbleText"))
    assert len(text) == MAX_BUBBLE_CHARS == 24_000 and text.endswith("…")
    bridge.push_delta("y" * 1000)
    bridge._flush_stream()
    assert len(str(bridge.property("bubbleText"))) == MAX_BUBBLE_CHARS
    bridge.set_bubble("z" * (MAX_BUBBLE_CHARS + 5))
    assert len(str(bridge.property("bubbleText"))) == MAX_BUBBLE_CHARS


def test_pieces_that_arrive_after_the_end_are_dropped(bridge: Bridge) -> None:
    bridge.push_delta("nobody asked")
    bridge._flush_stream()
    assert bridge.property("bubbleText") == ""

    bridge.begin_stream("Thinking…")
    bridge.push_delta("Hello")
    bridge.end_stream(note="[screen]", action="Do it")
    assert bridge.property("bubbleText") == "Hello", "pending pieces are shown at the end"
    assert (bridge.property("bubbleNote"), bridge.property("bubbleAction")) == ("[screen]", "Do it")
    assert bridge.property("bubbleBusy") is False
    bridge.push_delta(" late")
    bridge._flush_stream()
    assert bridge.property("bubbleText") == "Hello"

    bridge.begin_stream("Thinking…")
    assert (bridge.property("bubbleNote"), bridge.property("bubbleAction")) == ("", "")
    bridge.push_delta("half")
    bridge.set_bubble("Something else")  # a whole message replaces the stream
    bridge._flush_stream()
    assert bridge.property("bubbleText") == "Something else" and not bridge.streaming


def test_the_placeholder_is_replaced_by_the_first_words_or_the_fallback(bridge: Bridge) -> None:
    bridge.begin_stream("Thinking…")
    assert bridge.streaming and not bridge.stream_has_text
    bridge.end_stream(fallback="Nothing came.")
    assert bridge.property("bubbleText") == "Nothing came."

    bridge.begin_stream("Thinking…")
    bridge.end_stream()
    assert bridge.property("bubbleText") == "", "a stopped answer with no words leaves no bubble"

    bridge.begin_stream("Thinking…")
    bridge.push_delta("Words")
    assert bridge.stream_has_text
    bridge._flush_stream()
    assert bridge.property("bubbleText") == "Words"
    bridge.end_stream(fallback="Nothing came.")
    assert bridge.property("bubbleText") == "Words"


def test_stop_and_dismiss_ask_for_a_stop_only_while_streaming(bridge: Bridge) -> None:
    stops: list[int] = []
    bridge.stopRequested.connect(lambda: stops.append(1))
    bridge.set_bubble("hello")
    bridge.stopStream()
    bridge.dismissBubble()
    assert stops == [] and bridge.property("bubbleText") == ""

    bridge.begin_stream("Thinking…")
    bridge.stopStream()
    assert stops == [1] and bridge.streaming, "the owner decides what a stop does"
    bridge.dismissBubble()
    assert stops == [1, 1] and not bridge.streaming and bridge.property("bubbleText") == ""

    actions: list[int] = []
    bridge.bubbleActionRequested.connect(lambda: actions.append(1))
    bridge.bubbleActionClick()
    assert actions == [], "no button, no action"
    bridge.set_bubble("x", action="Go")
    bridge.bubbleActionClick()
    assert actions == [1]


def test_expanding_the_bubble_does_not_end_the_stream_or_lose_the_note(bridge: Bridge) -> None:
    bridge.begin_stream("Thinking…")
    bridge.push_delta("a")
    bridge.toggleBubble()
    assert bridge.streaming and bridge.property("bubbleExpanded") is True
    bridge._flush_stream()
    assert bridge.property("bubbleText") == "a"
    bridge.end_stream(note="[app]")
    bridge.toggleBubble()
    assert bridge.property("bubbleNote") == "[app]" and bridge.property("bubbleExpanded") is False


# ---- wording -------------------------------------------------------------------------------

CODES = (
    "busy",
    "model_unreachable",
    "model_missing",
    "model_timeout",
    "bad_model_output",
    "too_large",
    "bad_request",
    "internal",
    "brain_lost",
    "waking",
)


def test_every_error_code_has_its_own_short_plain_sentence() -> None:
    sentences = {code: error_text(code) for code in CODES}
    assert error_text("cancelled") == "", "stopping it yourself needs no comment"
    for code, sentence in sentences.items():
        assert 10 < len(sentence) < 90 and "\n" not in sentence, code
        assert "_" not in sentence, "a sentence, not a code"
    assert len(set(sentences.values())) == len(CODES)
    assert "waddle models pull all" in sentences["model_missing"]
    assert "still loading" in sentences["waking"]
    assert "went away" in sentences["brain_lost"]
    assert error_text("model_timeout", waking=True) == sentences["waking"]
    assert error_text("busy", waking=True) == sentences["busy"]
    assert error_text("some-new-code") == sentences["internal"]


def test_asleep_wording_names_the_commands_and_follows_the_reason() -> None:
    for state in (None, DOWN, LinkState("down", "connect_failed"), LinkState("down", "lost")):
        text = asleep_text(state)
        assert text == ASLEEP
        assert "asleep" in text and "waddle brain up" in text and "waddle doctor" in text
    token = asleep_text(LinkState("down", "bad_token"))
    assert "asleep" in token and "did not accept" in token and "waddle brain down" in token
    assert "waddle doctor" in token
    unknown = asleep_text(LinkState("down", "never-heard-of-it"))
    assert "asleep" in unknown and unknown.count("waddle doctor") == 1
    assert "never-heard-of-it" not in unknown
    assert "moment" in asleep_text(LinkState("connecting"))


def test_the_source_line_shows_only_known_labels_in_the_brains_order() -> None:
    assert source_line(("screen", "app")) == "[screen] [app]"
    assert source_line(("app", "screen", "app")) == "[app] [screen]"
    assert source_line(("model",)) == "[model]"
    assert source_line(("<b>x</b>", "system", 7)) == "[system]"
    assert source_line(()) == ""


def test_what_is_asked_about_a_version_is_cleaned_and_short() -> None:
    assert version_ask(NOTHING) is None
    assert version_ask(BLENDER._replace(needs_user=False)) is None
    assert version_ask(BLENDER) == VersionAsk(
        "org.blender.Blender", "Blender", ("Help", "About Blender")
    )
    sneaky = Current(AppContext(name="Evil\u202e\nApp"), None, True, ("He\u200blp", "\u200b"))
    ask = version_ask(sneaky)
    assert ask == VersionAsk("Evil App", "Evil App", ("Help",))
    assert version_ask(Current(AppContext(name="\u200b"), None, True, ())) is None
    hint = version_hint(VersionAsk("k", "Blender", ("Help", "About Blender")))
    assert "Blender" in hint and "Help → About Blender" in hint
    assert "usually" in version_hint(VersionAsk("k", "Blender", ()))


# ---- asking --------------------------------------------------------------------------------


def test_opening_the_ask_window_prewarms_and_a_question_reaches_the_asker(rig: Rig) -> None:
    rig.app.open_ask()
    assert rig.assist.asker.prewarms == 1
    rig.app._tray.ask.trigger()
    rig.app._on_hotkey("ask", True)
    rig.bridge.requestAsk()  # the penguin's own way in
    assert rig.assist.asker.prewarms == 4
    ask = rig.app._ask
    assert ask is not None and ask.isVisible()
    root_item = ask.rootObject()
    assert root_item is not None and root_item.property("hint") == ""
    root_item.submitted.emit("  what is this?  ")  # type: ignore[attr-defined]
    assert not ask.isVisible()
    assert rig.assist.asker.asked == ["what is this?"]
    assert rig.relay.turn == rig.app._turn == 1
    assert (rig.text, rig.busy, rig.mood) == (THINKING, True, Mood.THINKING)


def test_the_answer_streams_into_the_bubble_and_ends_with_its_sources(rig: Rig) -> None:
    rig.ask()
    rig.relay.delta("You are in ")
    rig.relay.delta("Blender.")
    assert rig.text == THINKING, "pieces wait for the flush"
    rig.flush()
    assert rig.text == "You are in Blender." and rig.busy
    rig.relay.delta(" Press Tab.")
    rig.relay.done(("screen", "app"), "smart_model")
    assert rig.text == "You are in Blender. Press Tab."
    assert (rig.note, rig.action, rig.busy) == ("[screen] [app]", "", False)
    assert rig.mood is Mood.IDLE
    bubble = rig.item("bubble")
    assert bubble.property("note") == "[screen] [app]" and bubble.property("busy") is False
    assert is_plain(rig.item("note"))


def test_an_answer_with_no_words_says_so(rig: Rig) -> None:
    rig.ask()
    rig.relay.done((), None)
    assert (rig.text, rig.note, rig.busy) == (NO_ANSWER, "", False)


def test_a_new_question_replaces_the_answer_and_old_events_are_dropped(rig: Rig) -> None:
    rig.ask("first")
    rig.relay.delta("Old answer.")
    rig.relay.done(("screen",), None)
    assert rig.note == "[screen]"

    events = rig.app._events
    rig.ask("second")
    assert (rig.text, rig.note, rig.busy) == (THINKING, "", True)
    # Events of the first question that were still queued when the second began:
    events.answerDelta.emit(1, "stale words")
    events.answerError.emit(1, "model_timeout")
    events.answerDone.emit(1, ("system",), None)
    rig.flush()
    assert (rig.text, rig.note, rig.busy) == (THINKING, "", True)
    assert rig.mood is Mood.THINKING
    rig.relay.delta("New answer.")
    rig.relay.done(("app",), None)
    assert (rig.text, rig.note) == ("New answer.", "[app]")


def test_stop_cancels_keeps_what_arrived_and_ignores_the_rest(rig: Rig) -> None:
    rig.ask()
    rig.relay.delta("Half an ans")
    rig.flush()
    stop = rig.item("stop")
    assert stop.property("visible") is True
    stop.clicked.emit()  # type: ignore[attr-defined]
    assert rig.assist.asker.cancels == 1
    assert (rig.text, rig.busy, rig.mood) == ("Half an ans", False, Mood.IDLE)
    assert rig.item("stop").property("visible") is False
    rig.relay.delta("wer that kept coming")
    rig.relay.error("cancelled")
    rig.relay.done(("screen",), None)
    rig.flush()
    assert (rig.text, rig.note) == ("Half an ans", "")


def test_stop_before_the_first_words_leaves_no_bubble(rig: Rig) -> None:
    rig.ask()
    rig.bridge.stopStream()
    assert (rig.text, rig.busy, rig.assist.asker.cancels) == ("", False, 1)


def test_closing_the_bubble_or_clicking_the_penguin_mid_answer_stops_it(rig: Rig) -> None:
    rig.ask()
    rig.relay.delta("Words")
    rig.flush()
    rig.item("close").clicked.emit()  # type: ignore[attr-defined]
    assert (rig.text, rig.busy, rig.assist.asker.cancels) == ("", False, 1)
    assert rig.mood is Mood.IDLE

    rig.ask()
    rig.relay.delta("Words")
    rig.flush()
    rig.bridge.bodyClick()
    assert (rig.text, rig.assist.asker.cancels) == ("", 2)


def test_a_cancel_from_the_brain_side_shows_nothing(rig: Rig) -> None:
    rig.ask()
    rig.relay.error("cancelled")
    assert (rig.text, rig.busy, rig.mood) == ("", False, Mood.IDLE)


@pytest.mark.parametrize("code", CODES)
def test_an_error_before_any_words_becomes_the_bubble(rig: Rig, code: str) -> None:
    rig.ask()
    rig.relay.error(code)
    assert (rig.text, rig.note, rig.busy) == (error_text(code), "", False)
    assert rig.mood is Mood.IDLE


def test_an_error_mid_answer_keeps_the_words_and_says_why_it_stopped(rig: Rig) -> None:
    rig.ask()
    rig.relay.delta("The first half")
    rig.relay.error("brain_lost")
    assert rig.text == "The first half" and rig.note == error_text("brain_lost")
    assert not rig.busy


def test_a_brain_that_goes_away_mid_answer_naps_at_once(rig: Rig) -> None:
    rig.ask()
    rig.relay.delta("Half")
    assert rig.mood is Mood.THINKING
    rig.app._on_link_state(LinkState("down", "lost"))
    assert rig.mood is Mood.NAPPING_BRAIN, "before the lost answer is even reported"
    rig.relay.error("brain_lost")
    assert (rig.text, rig.note, rig.mood) == ("Half", error_text("brain_lost"), Mood.NAPPING_BRAIN)


def test_a_timeout_while_a_model_is_waking_says_it_is_still_loading(rig: Rig) -> None:
    rig.app._on_link_state(LinkState("up", waking="smart_model"))
    rig.ask()
    rig.relay.error("model_timeout")
    assert rig.text == error_text("waking")


def test_a_question_before_the_assist_layer_is_wired_gets_an_honest_line(rig: Rig) -> None:
    rig.app._assist = None
    rig.ask()
    assert "starting" in rig.text and not rig.busy and rig.assist.asker.asked == []
    rig.app.open_ask()  # must not fail without an asker to prewarm
    assert rig.assist.asker.prewarms == 0


def test_asking_goes_through_while_quiet_or_paused_and_tips_stay_gated(rig: Rig) -> None:
    rig.app._set_quiet(True)
    assert not rig.app._may_volunteer("demo-greeting")
    rig.ask("quiet one")
    rig.relay.delta("Answer.")
    rig.relay.done((), None)
    assert rig.text == "Answer."

    rig.app._tray.pause.trigger()
    assert rig.mood is Mood.SLEEPING
    rig.ask("paused one")
    assert rig.assist.asker.asked == ["quiet one", "paused one"], "the asker handles blind asks"
    assert rig.mood is Mood.SLEEPING, "paused stays visible while he answers"
    rig.relay.delta("Blind answer.")
    rig.relay.done(("model",), None)
    assert (rig.text, rig.note, rig.mood) == ("Blind answer.", "[model]", Mood.SLEEPING)


def test_asking_brings_a_hidden_waddle_back_so_the_answer_can_be_seen(rig: Rig) -> None:
    rig.app._tray.show_waddle.setChecked(False)
    assert not rig.app._view.isVisible()
    rig.ask()
    assert rig.app._view.isVisible() and rig.app._tray.show_waddle.isChecked()

    rig.app._config = WaddleConfig.model_validate({"watching": {"fullscreen": "hide"}})
    full = replace(h.win(), fullscreen=True)
    rig.app._on_eyes(EyesState(blind=None, window=full, session=h.UNLOCKED))
    assert not rig.app._view.isVisible()
    rig.ask()
    assert rig.app._view.isVisible()


# ---- the brain is away -----------------------------------------------------------------------


def test_a_brain_that_is_down_shows_napping_unless_something_stronger_applies(rig: Rig) -> None:
    app = rig.app
    assert rig.mood is Mood.IDLE and app._tray.state is TrayState.WATCHING
    app._on_link_state(DOWN)
    assert rig.mood is Mood.NAPPING_BRAIN and app._tray.state is TrayState.BRAIN_DOWN
    assert rig.text == "", "going down volunteers no bubble"
    app._on_link_state(LinkState("connecting"))
    assert rig.mood is Mood.NAPPING_BRAIN, "a retry is not a recovery"
    app._on_link_state(LinkState("down", "connect_failed"))
    assert rig.mood is Mood.NAPPING_BRAIN

    blocked = EyesState(blind=BlindReason.BLOCKED, window=h.win(), session=h.UNLOCKED)
    app._on_eyes(blocked)
    assert rig.mood is Mood.EYES_CLOSED
    app._on_eyes(replace(blocked, blind=BlindReason.LOCKED))
    assert rig.mood is Mood.SLEEPING
    app._on_eyes(replace(blocked, blind=None))
    assert rig.mood is Mood.NAPPING_BRAIN

    app._tray.pause.trigger()
    assert rig.mood is Mood.SLEEPING and app._tray.state is TrayState.PAUSED
    app._tray.pause.trigger()
    assert rig.mood is Mood.NAPPING_BRAIN and app._tray.state is TrayState.BRAIN_DOWN

    app._on_link_state(UP)
    assert rig.mood is Mood.IDLE and app._tray.state is TrayState.WATCHING


def test_before_the_link_says_anything_he_does_not_nap(qtbot: Any, root: Path) -> None:
    app = WaddleApp(WaddleConfig(), short_paths(root))
    try:
        assert app._bridge.current_mood is Mood.IDLE
        app._on_link_state(LinkState("connecting"))
        assert app._bridge.current_mood is Mood.IDLE
        app._on_link_state(LinkState("down", "stopped"))  # Waddle's own doing, at shutdown
        assert app._bridge.current_mood is Mood.IDLE
    finally:
        app.shutdown()


def test_asking_while_the_brain_is_down_shows_the_asleep_bubble(rig: Rig) -> None:
    rig.app._on_link_state(DOWN)
    rig.assist.asker.script = lambda _text: rig.relay.error("brain_down")
    rig.ask()
    assert rig.assist.asker.asked == ["what is this?"], "the asker also retries the brain"
    assert rig.text == ASLEEP and "waddle brain up" in rig.text and "waddle doctor" in rig.text
    assert (rig.busy, rig.action, rig.mood) == (False, "", Mood.NAPPING_BRAIN)
    assert rig.item("action").property("visible") is False, "there is no Copy button"

    rig.app._on_link_state(UP)
    assert rig.text == "", "the brain is back: the line is no longer true"
    assert rig.mood is Mood.IDLE


def test_the_asleep_bubble_is_not_taken_back_if_something_else_is_showing(rig: Rig) -> None:
    rig.app._on_link_state(DOWN)
    rig.assist.asker.script = lambda _text: rig.relay.error("brain_down")
    rig.ask()
    rig.bridge.set_bubble("Something newer")
    rig.app._on_link_state(UP)
    assert rig.text == "Something newer"


def test_while_down_no_thinking_is_promised_but_a_brain_just_back_still_answers(rig: Rig) -> None:
    rig.app._on_link_state(DOWN)
    rig.ask()
    assert (rig.text, rig.busy, rig.mood) == ("", True, Mood.NAPPING_BRAIN)
    rig.relay.delta("It came back.")
    rig.relay.done((), None)
    assert rig.text == "It came back." and not rig.busy


# ---- which version is this? ------------------------------------------------------------------


def answer(rig: Rig, text: str = "what is this?") -> None:
    rig.ask(text)
    rig.relay.delta("An answer.")
    rig.relay.done(("screen",), None)


def test_an_unknown_version_is_asked_about_once_per_app(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    answer(rig)
    assert rig.action == VERSION_BUTTON
    assert rig.note == "[screen]\nI can't tell which version of Blender this is."
    assert rig.item("action").property("visible") is True

    answer(rig)
    assert (rig.action, rig.note) == ("", "[screen]"), "never nag: once per app"

    other = Current(AppContext(app_id="org.kde.krita", name="Krita"), None, True, ())
    rig.assist.identity.now = other
    answer(rig)
    assert rig.action == VERSION_BUTTON and "Krita" in rig.note

    rig.assist.identity.now = NOTHING  # a known version, a blocked window, a pause
    answer(rig)
    assert rig.action == ""


def test_the_button_opens_the_version_window_and_the_reply_goes_to_identity(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    answer(rig)
    rig.item("action").clicked.emit()  # type: ignore[attr-defined]
    ask = rig.app._ask
    assert ask is not None and ask.isVisible()
    root_item = ask.rootObject()
    assert root_item is not None
    hint = str(root_item.property("hint"))
    assert "Blender" in hint and "Help → About Blender" in hint
    label = root_item.findChild(QObject, "askHint")
    assert label is not None and label.property("visible") is True
    assert is_plain(label)
    assert rig.assist.asker.prewarms == 0, "no model is needed for this"

    root_item.submitted.emit("Blender 5.3")  # type: ignore[attr-defined]
    assert not ask.isVisible()
    assert rig.assist.identity.told == ["Blender 5.3"]
    assert rig.assist.asker.asked == ["what is this?"], "the reply is not sent as a question"
    assert rig.text == "Got it: Blender 5.3." and rig.action == ""

    rig.app.open_ask()
    assert root_item.property("hint") == "", "the next time it is an ordinary question again"
    assert root_item.findChild(QObject, "askHint").property("visible") is False


def test_a_reply_without_a_version_offers_another_try(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    rig.assist.identity.accepts = False
    answer(rig)
    rig.bridge.bubbleActionClick()
    root_item = rig.app._ask.rootObject()  # type: ignore[union-attr]
    root_item.submitted.emit("no idea")  # type: ignore[union-attr]
    assert (rig.text, rig.action) == (VERSION_UNREADABLE, VERSION_BUTTON)
    rig.assist.identity.accepts = True
    rig.bridge.bubbleActionClick()
    root_item.submitted.emit("5.3")  # type: ignore[union-attr]
    assert rig.assist.identity.told == ["no idea", "5.3"] and rig.text.startswith("Got it")


def test_a_version_is_never_applied_to_a_different_app_than_was_asked_about(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    answer(rig)
    rig.bridge.bubbleActionClick()
    # Before the reply, another app that also needs asking comes to the front.
    rig.assist.identity.now = Current(AppContext(app_id="k", name="Krita"), None, True, ())
    rig.app._ask.rootObject().submitted.emit("5.3")  # type: ignore[union-attr]
    assert rig.assist.identity.told == []
    assert (rig.text, rig.action) == (VERSION_MOVED, "")


def test_the_tray_offers_the_version_window_only_while_it_is_wanted(rig: Rig) -> None:
    tray = rig.app._tray
    assert "Tell Waddle the version…" not in tray.action_texts()
    tray.tell_version.trigger()
    assert rig.app._ask is None, "nothing to ask about: nothing opens"

    rig.assist.identity.now = BLENDER
    rig.app._eyes_from_io(EyesState(blind=None, window=h.win(), session=h.UNLOCKED))
    assert "Tell Waddle the version…" in tray.action_texts()
    answer(rig)
    answer(rig)
    assert rig.action == "", "asked once already"
    tray.tell_version.trigger()  # you open it yourself: always allowed
    ask = rig.app._ask
    assert ask is not None and ask.isVisible()
    assert "Blender" in str(ask.rootObject().property("hint"))  # type: ignore[union-attr]
    ask.rootObject().submitted.emit("5.3")  # type: ignore[union-attr]
    assert rig.assist.identity.told == ["5.3"]
    assert "Tell Waddle the version…" not in tray.action_texts()


def test_the_identity_signal_is_sent_only_when_the_question_changes(rig: Rig) -> None:
    seen: list[object] = []
    rig.app._events.identity.connect(seen.append)
    rig.relay.identity_changed()
    assert seen == []
    rig.assist.identity.now = BLENDER
    rig.relay.identity_changed()
    rig.relay.identity_changed()
    assert seen == [VersionAsk("org.blender.Blender", "Blender", ("Help", "About Blender"))]
    rig.assist.identity.now = NOTHING
    rig.relay.identity_changed()
    assert seen[-1] is None and len(seen) == 2


# ---- untrusted text ------------------------------------------------------------------------


def texts_in(item: QObject) -> list[QObject]:
    return [
        child
        for child in item.findChildren(QQuickItem)
        if child.metaObject().className() == "QQuickText"
    ]


def test_a_canary_in_an_answer_is_plain_text_and_never_logged(rig: Rig) -> None:
    with h.captured_logs() as lines:
        rig.app.open_ask()
        rig.app._ask.rootObject().submitted.emit(f"question {CANARY}")  # type: ignore[union-attr]
        rig.bridge.submitText("CANARY-one-word")  # short enough to pass the log's own filter
        rig.relay.delta(CANARY)
        rig.relay.delta("\u202e hidden \u200b")
        rig.relay.done(("screen",), None)
        rig.relay.error("internal")
    assert rig.text == CANARY + " hidden ", "shown as it is, minus invisible characters"
    bubble = rig.item("bubble")
    assert bubble.property("text") == rig.text
    labels = [t for t in texts_in(bubble) if t.property("text") == rig.text]
    assert len(labels) == 1 and is_plain(labels[0])
    assert len(texts_in(bubble)) >= 5 and all(is_plain(t) for t in texts_in(bubble))
    assert lines, "the run did log something"
    assert not any("CANARY" in line or "hidden" in line or "example" in line for line in lines)


def test_a_hostile_app_name_stays_plain_and_out_of_the_logs(rig: Rig) -> None:
    name = "<img src=x> CANARY-app"
    rig.assist.identity.now = Current(AppContext(app_id="x", name=name), None, True, ("<u>Help",))
    with h.captured_logs() as lines:
        answer(rig)
        rig.bridge.bubbleActionClick()
        rig.app._ask.rootObject().submitted.emit("CANARY-version 5.3")  # type: ignore[union-attr]
    assert name in rig.note or name in rig.text
    hint = rig.app._ask.rootObject().findChild(QObject, "askHint")  # type: ignore[union-attr]
    assert is_plain(hint)
    assert not any("CANARY" in line or "img" in line for line in lines)


def test_every_text_item_this_part_added_is_plain_text() -> None:
    qml = Path(app_module.QML_DIR)
    for name in ("Bubble.qml", "Ask.qml"):
        source = (qml / name).read_text(encoding="utf-8")
        assert source.count("textFormat: Text.PlainText") == source.count("    Text {"), name
        for forbidden in ("RichText", "StyledText", "MarkdownText", "linkActivated", "openUrl"):
            assert forbidden not in source, (name, forbidden)


# ---- threads ---------------------------------------------------------------------------------


def test_signals_from_the_io_thread_are_delivered_on_the_ui_thread(qtbot: Any, root: Path) -> None:
    app = WaddleApp(WaddleConfig(), short_paths(root))
    fake = FakeAssist()
    fake.identity.on_change = app._relay.identity_changed
    app._assist = fake  # type: ignore[assignment]
    app._relay.assist = fake  # type: ignore[assignment]
    emitted_on: list[str] = []
    handled_on: list[tuple[str, str]] = []

    def record(name: str) -> Callable[..., None]:
        return lambda *_args: handled_on.append((name, threading.current_thread().name))

    events = app._events
    for name in ("linkState", "answerDelta", "answerDone", "answerError", "identity"):
        getattr(events, name).connect(record(name))
    events.versionApplied.connect(record("versionApplied"))

    def from_io() -> None:
        emitted_on.append(threading.current_thread().name)
        events.linkState.emit(UP)
        app._relay.turn = 1
        app._relay.delta("hello")
        app._relay.done(("screen",), None)
        app._relay.error("internal")
        fake.identity.now = BLENDER
        app._relay.identity_changed()
        app._relay.apply_version("org.blender.Blender", "5.3")

    try:
        app._io.start()
        app._turn = 1
        app._bridge.begin_stream(THINKING)
        app._io.submit(asyncio.sleep(0)).result(2)
        app._io.call(from_io)
        app._io.submit(asyncio.sleep(0)).result(2)  # from_io has run, on the other thread
        assert emitted_on == ["waddle-io"]
        assert handled_on == [], "queued: nothing is handled until the UI loop turns"
        assert app._link is None and app._bridge.property("bubbleText") == THINKING
        qtbot.waitUntil(lambda: len(handled_on) == 7, timeout=3000)
        main = threading.main_thread().name
        assert handled_on == [
            ("linkState", main),
            ("answerDelta", main),
            ("answerDone", main),
            ("answerError", main),
            ("identity", main),
            ("identity", main),
            ("versionApplied", main),
        ]
        assert app._link == UP and app._version_ask is None  # asked, then answered with 5.3
        assert fake.identity.told == ["5.3"]
    finally:
        app.shutdown()


def test_end_to_end_with_a_fake_brain_on_a_real_socket(
    qtbot: Any, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = short_paths(root)
    monkeypatch.setattr(
        app_module,
        "build_assist",
        partial(build_assist, packs=h.fixture_packs(), sandbox=SharedExec(lambda: h.FakeExec())),
    )
    app = WaddleApp(WaddleConfig(), paths)
    bridge = app._bridge
    brain = fb.FakeBrain(paths.brain_socket, ensure_token(paths.link_token))

    async def on_ask(session: fb.Session, ask: Ask) -> None:
        await session.send(AnswerDelta(id=ask.id, text=f"Hello {CANARY}"))
        await session.send(AnswerDelta(id=ask.id, text=" and more."))
        await session.send(AnswerDone(id=ask.id, sources=("model",)))

    brain.on_ask = on_ask
    with h.captured_logs() as lines:
        try:
            app._io.start()
            app._io.call(app._senses.session_changed, h.UNLOCKED)
            app._io.submit(brain.start()).result(5)
            app._io.submit(app._start_assist()).result(5)
            assert app._assist is not None
            qtbot.waitUntil(lambda: app._link == LinkState("up", models=fb.MODELS), timeout=5000)
            assert bridge.current_mood is Mood.IDLE

            bridge.submitText("what is this?")
            assert bridge.current_mood is Mood.THINKING
            qtbot.waitUntil(lambda: bridge.property("bubbleNote") == "[model]", timeout=5000)
            assert bridge.property("bubbleText") == f"Hello {CANARY} and more."
            assert bridge.property("bubbleBusy") is False
            asks = brain.of_type(Ask)
            assert len(asks) == 1 and asks[0].text == "what is this?"
            assert asks[0].can_see is False, "no capture helper here: asked without a picture"

            # Pull the plug: napping, no crash, and asking says how to start it.
            app._io.submit(brain.stop()).result(5)
            qtbot.waitUntil(lambda: bridge.current_mood is Mood.NAPPING_BRAIN, timeout=5000)
            bridge.submitText("still there?")
            qtbot.waitUntil(lambda: bridge.property("bubbleText") == ASLEEP, timeout=5000)
            assert bridge.property("bubbleBusy") is False
            assert bridge.current_mood is Mood.NAPPING_BRAIN

            # And back: the link reconnects on its own and the asleep line goes away.
            app._io.submit(brain.start()).result(5)
            app._io.call(app._assist.link.kick)
            qtbot.waitUntil(lambda: bridge.current_mood is Mood.IDLE, timeout=8000)
            assert bridge.property("bubbleText") == ""
        finally:
            app.shutdown()
            assert app._assist is None or app._assist.link.state == LinkState("down", "stopped")
    assert not app._io.running
    assert not any("CANARY" in line or "what is this" in line for line in lines)


# ---- found in review -------------------------------------------------------------------------


def test_an_answer_of_blank_lines_counts_as_no_answer(rig: Rig) -> None:
    rig.ask()
    rig.relay.delta("\n \n")
    rig.relay.done(("model",), None)
    assert (rig.text, rig.busy) == (NO_ANSWER, False), "not an empty-looking bubble"

    rig.ask()
    rig.relay.delta("\n\n")
    rig.relay.error("model_timeout")
    assert (rig.text, rig.note) == (error_text("model_timeout"), ""), "the reason is not lost"

    rig.ask()
    rig.relay.delta("\n")
    rig.flush()
    rig.bridge.stopStream()
    assert rig.text == "", "stopped with nothing to read: no bubble"

    rig.ask()
    rig.relay.delta("\n")
    rig.relay.delta("Words after a blank line")
    rig.relay.error("brain_lost")
    assert rig.text.strip() == "Words after a blank line"
    assert rig.note == error_text("brain_lost")


def test_if_the_answering_part_failed_to_start_he_does_not_say_still_starting(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = rig.app
    app._assist = None
    rig.ask()
    assert rig.text == STARTING

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("CANARY-start /home/someone/packs")

    monkeypatch.setattr(app_module, "build_assist", broken)
    with h.captured_logs() as lines:
        asyncio.run(app._start_assist())
    assert app._assist is None
    assert any("assist.start_failed" in line for line in lines)
    assert not any("CANARY" in line for line in lines)
    rig.ask()
    assert rig.text == START_FAILED != STARTING
    assert "waddle doctor" in rig.text and "moment" not in rig.text


def test_events_that_arrive_after_stop_change_nothing(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    blender = version_ask(BLENDER)
    events = rig.app._events
    rig.ask()
    rig.relay.delta("Half")
    rig.flush()
    rig.bridge.stopStream()
    # Still queued when Stop was clicked: the end of that same question.
    events.answerError.emit(rig.app._turn, "brain_down")
    events.answerDone.emit(rig.app._turn, ("screen",), blender)
    assert (rig.text, rig.note, rig.action) == ("Half", "", "")
    assert rig.app._asleep_shown == ""
    answer(rig)
    assert rig.action == VERSION_BUTTON, "the one time to ask about Blender was not used up"


def test_telling_the_version_mid_answer_stops_that_answer(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    rig.app._eyes_from_io(EyesState(blind=None, window=h.win(), session=h.UNLOCKED))
    rig.ask()
    rig.relay.delta("A long answer")
    assert rig.mood is Mood.THINKING
    rig.app._tray.tell_version.trigger()
    rig.app._ask.rootObject().submitted.emit("5.3")  # type: ignore[union-attr]
    assert rig.text.startswith("Got it")
    assert rig.assist.asker.cancels == 1, "the answer nobody will see is not left running"
    assert rig.mood is Mood.IDLE, "and he does not go on looking as if he were thinking"


def test_a_version_told_while_paused_gets_a_true_reason(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    answer(rig)
    rig.bridge.bubbleActionClick()
    rig.app._tray.pause.trigger()
    rig.assist.identity.now = NOTHING  # paused: nothing is known about the app in front
    rig.app._ask.rootObject().submitted.emit("5.3")  # type: ignore[union-attr]
    assert rig.assist.identity.told == []
    assert rig.text == VERSION_PAUSED and "paused" in rig.text
    assert "Click on it" not in rig.text


def test_a_long_version_reply_is_cut_before_it_goes_anywhere(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    answer(rig)
    rig.bridge.bubbleActionClick()
    rig.app._ask.rootObject().submitted.emit("5.3 " + "x" * 5000)  # type: ignore[union-attr]
    assert [len(text) for text in rig.assist.identity.told] == [200]


def test_the_version_window_does_not_keep_half_a_question(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    rig.app.open_ask()
    root_item = rig.app._ask.rootObject()  # type: ignore[union-attr]
    field = root_item.findChild(QObject, "askField")  # type: ignore[union-attr]
    field.setProperty("text", "how do I")
    rig.app.open_ask()
    assert field.property("text") == "how do I", "the same window again keeps what you typed"
    answer(rig)
    rig.bridge.bubbleActionClick()
    assert field.property("text") == "", "half a question is not a version"
    field.setProperty("text", "5.")
    rig.app.open_ask()
    assert field.property("text") == "", "and half a version is not a question"


def test_asking_closes_the_guide_card_so_the_answer_can_be_seen(rig: Rig) -> None:
    from waddle_core.guide import mock_guide

    rig.bridge.set_guide(mock_guide(), card_open=True)
    assert rig.item("bubble").property("visible") is False
    rig.ask()
    assert rig.bridge.property("cardOpen") is False
    assert rig.item("bubble").property("visible") is True and rig.text == THINKING


def test_a_finished_answer_offers_no_stop_next_to_its_button(rig: Rig) -> None:
    rig.assist.identity.now = BLENDER
    rig.ask()
    rig.relay.delta("Words")
    rig.flush()
    assert rig.item("stop").property("visible") is True
    assert rig.item("action").property("visible") is False
    rig.relay.done((), None)
    assert rig.item("action").property("visible") is True
    assert rig.item("stop").property("visible") is False


def test_a_failure_while_asking_ends_the_question_and_logs_no_text(rig: Rig) -> None:
    def broken(_text: str) -> None:
        raise RuntimeError("CANARY-ask")

    rig.assist.asker.script = broken
    with h.captured_logs() as lines:
        rig.ask()
    assert (rig.text, rig.busy, rig.mood) == (error_text("internal"), False, Mood.IDLE)
    assert any("ask_failed" in line for line in lines)
    assert not any("CANARY" in line for line in lines)


def test_at_shutdown_the_open_question_and_the_link_stop_before_the_senses(
    qtbot: Any, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = WaddleApp(WaddleConfig(), short_paths(root))
    order: list[str] = []

    def stopper(name: str) -> Callable[[], Coroutine[Any, Any, None]]:
        async def stop() -> None:
            order.append(name)

        return stop

    class Stoppable:
        stop = staticmethod(stopper("assist"))

    try:
        for name in ("_hotkeys", "_session", "_senses", "_kwin"):
            monkeypatch.setattr(getattr(app, name), "stop", stopper(name))
        app._assist = Stoppable()  # type: ignore[assignment]
        asyncio.run(app._stop_sensors())
        assert order[0] == "assist" and set(order[1:]) == {
            "_hotkeys",
            "_session",
            "_senses",
            "_kwin",
        }
    finally:
        app._assist = None
        app.shutdown()


def test_an_error_nobody_caught_on_the_io_loop_is_logged_without_its_text(
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An earlier test may have left the logger writing to a stream that is closed by now; a
    # failed write makes ``logging`` print the error being handled, which is not under test.
    monkeypatch.setattr(logging.getLogger("waddle"), "handlers", [])

    def boom() -> None:
        raise ValueError("CANARY-title /home/someone/secret.txt")

    async def bad_task() -> None:
        raise RuntimeError("CANARY-task")

    async def spawn() -> None:
        task = asyncio.get_running_loop().create_task(bad_task())
        await asyncio.sleep(0.01)
        del task

    io = IoThread()
    caplog.set_level(logging.DEBUG)
    with h.captured_logs() as lines:
        io.start()
        try:
            io.call(boom)
            io.submit(spawn()).result(2)
            io.submit(asyncio.sleep(0.05)).result(2)
        finally:
            io.stop()
    errors = [line for line in lines if '"io_error"' in line]
    assert len(errors) >= 1, lines
    assert '"kind":"ValueError"' in errors[0] and '"where":"test_ui_ask:' in errors[0]
    out, err = capfd.readouterr()
    assert "CANARY" not in out + err + caplog.text + "".join(lines)


def test_an_error_nobody_caught_on_the_ui_thread_is_logged_without_its_text(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # put back afterwards
    app_module.install_error_hook()
    try:
        raise KeyError("CANARY-answer text")
    except KeyError as exc:
        caught = exc
    with h.captured_logs() as lines:
        sys.excepthook(type(caught), caught, caught.__traceback__)
    assert len(lines) == 1 and '"ui_error"' in lines[0] and '"kind":"KeyError"' in lines[0]
    assert '"where":"test_ui_ask:' in lines[0]
    out, err = capfd.readouterr()
    assert "CANARY" not in out + err + lines[0]
    assert error_site(None) == "unknown"


def test_this_module_reads_the_flush_interval_it_tests() -> None:
    assert 20 <= bridge_module.STREAM_FLUSH_MS <= 150, "a few updates a second, not per token"
