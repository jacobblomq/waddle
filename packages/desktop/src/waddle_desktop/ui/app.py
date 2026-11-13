"""The desktop application: wires the bridge, windows, tray, theme and KWin link together."""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import socket
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from types import TracebackType
from typing import Final

from PySide6.QtCore import QObject, QRect, QSocketNotifier, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QRegion, QSurfaceFormat
from PySide6.QtQuick import QQuickView
from PySide6.QtWidgets import QApplication

import waddle_desktop
from waddle_core.capabilities import Capabilities, degradations
from waddle_core.config import WaddleConfig
from waddle_core.identity import APP_ID, PENGUIN_WINDOW_TITLE
from waddle_core.link import MAX_DELTA_CHARS
from waddle_core.mood import Mood
from waddle_core.paths import WaddlePaths
from waddle_core.privacy import BlindReason, load_blocklist
from waddle_core.proactivity import Context, Decision, Kind, TipBudget, decide
from waddle_core.untrusted import clean
from waddle_core.watching import CaptureScheduler, WatchPolicy
from waddle_core.window import WindowInfo
from waddle_desktop import log
from waddle_desktop.assist import Assist, Current, build_assist
from waddle_desktop.identity.cache import AppIdentity, IdentityCache, identify
from waddle_desktop.identity.packaging import RealFs
from waddle_desktop.identity.snapshot import ProcReader
from waddle_desktop.io_thread import HostEvents, IoThread, error_site
from waddle_desktop.kwin.link import KWinLink
from waddle_desktop.kwin.scripts import core_script_path, cursor_script_path
from waddle_desktop.kwin.types import OutputInfo, OwnWindow
from waddle_desktop.link.client import LinkState
from waddle_desktop.placement.positions import (
    PositionStore,
    Rect,
    clamp_to_output,
    default_position,
)
from waddle_desktop.resources import data_base, default_blocklist_path
from waddle_desktop.sensors.capture_client import Frame
from waddle_desktop.sensors.hotkey_setup import bind, make_plan, shortcut_labels
from waddle_desktop.sensors.hotkeys import PortalHotkeys
from waddle_desktop.sensors.senses import EyesState, Senses, open_helper_eye
from waddle_desktop.sensors.session import SessionWatcher
from waddle_desktop.theme.kdeglobals import kdeglobals_paths, load_palette
from waddle_desktop.theme.palette import ThemePalette
from waddle_desktop.theme.watcher import ThemeWatcher
from waddle_desktop.ui.bridge import Bridge
from waddle_desktop.ui.demo import DemoDriver
from waddle_desktop.ui.dodge import DodgeMachine
from waddle_desktop.ui.eyes import EyesModel, FrameProvider
from waddle_desktop.ui.theme_object import ThemeObject
from waddle_desktop.ui.tray import Tray, TrayState

_PACKAGE_DIR = Path(waddle_desktop.__file__).parent
QML_DIR = _PACKAGE_DIR / "qml"


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _client_version() -> str:
    try:
        return metadata.version("waddle-desktop")[:32]
    except metadata.PackageNotFoundError:
        return "0"


# ---- what Waddle says around an answer ---------------------------------------------------
#
# Everything here is first-party wording picked by a code. Text from the machine (an app's
# name) is cleaned to one short line first, and all of it is shown as plain text.

THINKING: Final = "Thinking…"
STARTING: Final = "I'm still starting up. Ask me again in a moment."
START_FAILED: Final = (
    "I couldn't start the part of me that answers. Restart Waddle, or run: waddle doctor"
)
NO_ANSWER: Final = "I have no answer for that."
VERSION_BUTTON: Final = "Tell Waddle the version"
VERSION_PLACEHOLDER: Final = "The version, for example 5.3"
ASK_PLACEHOLDER: Final = "Ask Waddle…"
VERSION_UNREADABLE: Final = "I couldn't find a version number in that. It looks like 5.3 or 21.0.2."
VERSION_MOVED: Final = "That app is no longer in front. Click on it, then tell me again."
VERSION_PAUSED: Final = (
    "I'm paused, so I can't tell which app is in front. Unpause me, then tell me again."
)
SOURCE_LABELS: Final = ("screen", "system", "app", "model")
_MAX_VERSION_TEXT: Final = 200

_NOT_RUNNING: Final = frozenset({"", "stopped", "no_socket", "connect_failed", "lost"})
ASLEEP: Final = (
    "My brain is asleep.\nTo start it, run: waddle brain up\nTo find out more, run: waddle doctor"
)
_STILL_LOADING: Final = "My model is still loading. Try again in a moment."
_ERRORS: Final[dict[str, str]] = {
    "brain_lost": "My brain went away in the middle of that answer. Ask again in a moment.",
    "model_missing": "A model I need is not downloaded yet. Run: waddle models pull all",
    "model_unreachable": "I can't reach my models. Run: waddle doctor",
    # UNVERIFIED (S3.2): the brain marks a timeout during a model load as "waking", but the
    # link client does not pass that mark on, so a plain timeout has to cover both.
    "model_timeout": "My model took too long. It may still be loading. Try again in a moment.",
    "waking": _STILL_LOADING,
    "busy": "I'm still busy with another question. Try again in a moment.",
    "bad_model_output": "My model gave an answer I can't use. Try asking again.",
    "too_large": "That question or its answer was too long for me. Try a shorter one.",
    "bad_request": "I couldn't send that question. Try wording it differently.",
}
_ERROR_OTHER: Final = "Something went wrong on my side. Run: waddle doctor"


def asleep_text(link: LinkState | None) -> str:
    """What to say when you ask and the brain is not there. Wording per ``LinkState.reason``."""
    if link is None or (link.status == "down" and link.reason in _NOT_RUNNING):
        return ASLEEP
    if link.status != "down":
        return "I'm still reaching my brain. Ask me again in a moment."
    off = degradations(Capabilities(brain="down", brain_reason=link.reason))[0]
    more = "" if "waddle doctor" in off.fix else "\nTo find out more, run: waddle doctor"
    return f"My brain is asleep. {off.why}\n{off.fix}{more}"


def error_text(code: str, *, waking: bool = False) -> str:
    """One plain sentence for an answer that failed. Empty for one you stopped yourself."""
    if code == "cancelled":
        return ""
    if waking and code == "model_timeout":
        return _STILL_LOADING
    return _ERRORS.get(code, _ERROR_OTHER)


def source_line(sources: Iterable[object]) -> str:
    """ "[screen] [app]": where the answer came from, in the order the brain gave."""
    known = [label for label in dict.fromkeys(sources) if label in SOURCE_LABELS]
    return " ".join(f"[{label}]" for label in known)


def _line(text: str, limit: int) -> str:
    return " ".join(clean(text, limit).split())


@dataclass(frozen=True)
class VersionAsk:
    """An app whose version Waddle could not work out, as the UI needs it."""

    key: str  # which app, to ask about it only once; never shown and never logged
    name: str
    about_path: tuple[str, ...]  # menu labels from the app's pack; may be empty


def version_ask(current: Current) -> VersionAsk | None:
    app = current.app
    if not current.needs_user or app is None:
        return None
    name = _line(app.name, 64)
    if not name:
        return None
    path = tuple(label for label in (_line(part, 48) for part in current.about_path) if label)
    return VersionAsk(app.app_id or name, name, path[:6])


def version_hint(ask: VersionAsk) -> str:
    if ask.about_path:
        return (
            f"Which version of {ask.name} is this? It is shown under {' → '.join(ask.about_path)}."
        )
    return f"Which version of {ask.name} is this? It is usually shown under Help → About."


class _Relay:
    """Lives on the I/O thread: turns what the assist layer reports into queued signals."""

    def __init__(self, events: HostEvents):
        self._events = events
        self.turn = 0  # the UI's number for the open question; set on the loop with each ask
        self.assist: Assist | None = None
        self._told: VersionAsk | None = None

    def delta(self, text: str) -> None:
        self._events.answerDelta.emit(self.turn, text)

    def done(self, sources: tuple[str, ...], _role: str | None) -> None:
        self._events.answerDone.emit(self.turn, sources, self.version_ask())

    def error(self, code: str) -> None:
        self._events.answerError.emit(self.turn, code)

    def identity_changed(self) -> None:
        """The window in front, or what is known about it, changed."""
        ask = self.version_ask()
        if ask != self._told:
            self._told = ask
            self._events.identity.emit(ask)

    def version_ask(self) -> VersionAsk | None:
        assist = self.assist
        if assist is None:
            return None
        try:
            return version_ask(assist.identity.current())
        except Exception as exc:
            log.warning("version_ask_failed", kind=type(exc).__name__)
            return None

    def apply_version(self, key: str, text: str) -> None:
        """You typed the version of the app ``key``. Says how it went with a signal."""
        outcome, label = "moved", ""
        assist = self.assist
        try:
            ask = self.version_ask()
            if assist is not None and ask is not None and ask.key == key:
                outcome = "unreadable"
                if assist.identity.apply_user_version(text):
                    outcome = "ok"
                    app = assist.identity.current().app
                    if app is not None and app.version:
                        label = _line(f"{app.name} {app.version}", 100)
        except Exception as exc:  # the text of the error could quote what you typed
            log.warning("version_apply_failed", kind=type(exc).__name__)
        self._events.versionApplied.emit(outcome, label)


class PenguinView(QQuickView):
    """The penguin window. Reports exposure so animations stop when nobody can see them."""

    def __init__(self, bridge: Bridge):
        super().__init__()
        self._bridge = bridge
        self._rects: list[QRect] = []
        self.setTitle(PENGUIN_WINDOW_TITLE)
        self.setColor(QColor(Qt.GlobalColor.transparent))
        self.setFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setResizeMode(QQuickView.ResizeMode.SizeViewToRootObject)
        self.rootContext().setContextProperty("bridge", bridge)
        bridge.inputRectsChanged.connect(self._set_rects)
        bridge.dragRequested.connect(self.startSystemMove)
        self.visibleChanged.connect(self._apply_mask)

    def _set_rects(self, rects: list[QRect]) -> None:
        self._rects = rects
        self._apply_mask()

    def _apply_mask(self) -> None:
        # Re-applied on every show: Qt does not re-send the input region after hide/show
        # (QTBUG-62638). An empty region would mean "whole window", so keep a 1px stub.
        region = QRegion()
        for rect in self._rects:
            region = region.united(rect)
        if region.isEmpty():
            region = QRegion(0, 0, 1, 1)
        self.setMask(region)

    def teardown(self) -> None:
        """Destroy the QML scene while the bridge it binds to is still alive."""
        self.hide()
        self.setSource(QUrl())

    def exposeEvent(self, event: object) -> None:
        super().exposeEvent(event)  # type: ignore[arg-type]
        self._bridge.set_view(running=self.isExposed())


class WaddleApp(QObject):
    def __init__(self, config: WaddleConfig, paths: WaddlePaths, *, demo: bool = False):
        super().__init__()
        self._config = config
        self._paths = paths
        self._paused = False
        self._quiet = False
        self._user_visible = True
        self._brought_back = False
        self._base_mood = Mood.IDLE
        self._eyes_state = EyesState()
        self._sensors_reported = False  # until then the penguin does not act blind
        self._tips = TipBudget()
        self._steady = Mood.IDLE
        # ---- the brain and the open question (all UI-thread state) ----
        self._assist: Assist | None = None  # set once, from the I/O loop, when it is wired
        self._assist_failed = False  # set from the I/O loop if the wiring itself failed
        self._turn = 0  # counts questions; events of an older one are dropped
        self._asking = False  # a question is open and the brain was up when it was asked
        self._link: LinkState | None = None  # None until the link has said anything
        self._brain_down = False
        self._asleep_shown = ""  # the "brain is asleep" bubble, to take it back when untrue
        self._version_ask: VersionAsk | None = None  # the app in front, if it needs asking
        self._bubble_version: VersionAsk | None = None  # what the bubble's button asks about
        self._ask_version: VersionAsk | None = None  # set while the Ask window asks a version
        self._version_replied: VersionAsk | None = None  # the app your last reply was about
        self._version_asked: set[str] = set()  # apps already asked about in this session
        self._outputs: dict[str, Rect] = {}
        self._placed = False
        self._last_own: OwnWindow | None = None
        self._own_uuid: str | None = None
        self._move_target: tuple[int, int] | None = None
        self._pending_save: tuple[Rect, str, tuple[int, int]] | None = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._flush_position)

        env, home = dict(os.environ), Path.home()
        reduced = config.ui.reduced_motion

        def load() -> ThemePalette:
            return load_palette(env, home, reduced)

        self._theme = ThemeObject(load())
        self._watcher = ThemeWatcher(kdeglobals_paths(env, home), load, parent=self)
        self._watcher.changed.connect(self._on_palette)

        self._bridge = Bridge(self._theme, self)
        self._bridge.set_view(
            size_scale=config.ui.size_scale,
            style=config.ui.style,
            motion_scale=self._theme.palette.animation_factor,
            bubble_max_lines=config.ui.bubble_max_lines,
        )
        self._view = PenguinView(self._bridge)
        self._view.setSource(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
        self._ask: QQuickView | None = None

        self._tray = Tray(self._theme.palette.accent, demo=demo, parent=self)
        self._tray.pauseToggled.connect(self._set_paused)
        self._tray.visibilityToggled.connect(self._set_user_visible)
        self._tray.quietToggled.connect(self._set_quiet)
        self._tray.eyesRequested.connect(self.open_eyes)
        self._tray.askRequested.connect(self.open_ask)
        self._tray.versionRequested.connect(self.open_version)
        self._tray.quitRequested.connect(QApplication.quit)
        self._demo = DemoDriver(self._bridge, self) if demo else None
        if self._demo is not None:
            self._tray.demoRequested.connect(self._demo.handle)

        self._dodge = DodgeMachine(
            dwell_ms=config.placement.dodge_dwell_ms,
            return_ms=config.placement.dodge_return_ms,
            enabled=config.placement.dodge,
        )
        self._dodge_timer = QTimer(self)
        self._dodge_timer.setSingleShot(True)
        self._dodge_timer.timeout.connect(self._dodge_tick)
        b = self._bridge
        b.bodyHoverChanged.connect(self._on_hover)
        b.bodyPressed.connect(lambda: self._dodge_event("press"))
        b.bodyClicked.connect(self._on_click)
        b.bubbleChanged.connect(self._sync_blocked)
        b.guideChanged.connect(self._sync_blocked)
        b.moodChanged.connect(self._on_mood)
        b.transientFinished.connect(lambda: b.set_mood(self._steady))
        b.askRequested.connect(self.open_ask)
        b.stopRequested.connect(self._on_stop)
        b.bubbleActionRequested.connect(self._on_bubble_action)
        if self._demo is None:  # in demo mode the demo driver plays the brain's part
            b.textSubmitted.connect(self._ask_question)

        self._positions = PositionStore(paths.desktop_db)
        self._events = HostEvents()
        self._events.ownWindow.connect(self._on_own_window)
        self._events.outputs.connect(self._on_outputs)
        self._events.kwinStatus.connect(self._on_kwin_status)
        self._events.eyes.connect(self._on_eyes)
        self._events.frame.connect(self._on_frame)
        self._events.hotkey.connect(self._on_hotkey)
        self._events.hotkeysBound.connect(self._tray.set_shortcuts)
        self._events.linkState.connect(self._on_link_state)
        self._events.answerDelta.connect(self._on_answer_delta)
        self._events.answerDone.connect(self._on_answer_done)
        self._events.answerError.connect(self._on_answer_error)
        self._events.identity.connect(self._on_identity)
        self._events.versionApplied.connect(self._on_version_applied)
        self._relay = _Relay(self._events)
        self._io = IoThread()

        # ---- senses: these objects live on the I/O thread once it starts ----
        self._frames = FrameProvider()
        self._eyes_model = EyesModel(self._frames, self)
        self._eyes_model.lookRequested.connect(self._look_now)
        self._eyes_view: QQuickView | None = None
        # No readable block list means no start: Waddle never looks without one.
        blocklist = load_blocklist(default_blocklist_path()).merged(
            config.privacy.block_classes, config.privacy.block_titles
        )
        self._identity_cache = IdentityCache(paths.identity_cache)
        reader, fs = ProcReader(), RealFs()

        def identify_window(window: WindowInfo) -> AppIdentity:
            return identify(
                window.pid,
                reader,
                fs,
                home,
                self._identity_cache,
                window_class=window.resource_class,
                desktop_file=window.desktop_file,
            )

        self._senses = Senses(
            blocklist=blocklist,
            scheduler=CaptureScheduler(WatchPolicy.from_config(config.watching)),
            eye_factory=lambda: open_helper_eye(paths.capture_bin),
            identify=identify_window,
            on_state=self._eyes_from_io,
            on_frame=self._events.frame.emit,
        )
        self._session = SessionWatcher(on_change=self._senses.session_changed)
        self._hotkeys = PortalHotkeys(
            on_pressed=lambda action: self._events.hotkey.emit(action, True),
            on_released=lambda action: self._events.hotkey.emit(action, False),
        )
        base = data_base()
        self._kwin = KWinLink(
            paths.runtime_dir,
            core_script_path(base),
            cursor_script=cursor_script_path(base),
            on_own_window=self._events.ownWindow.emit,
            on_outputs=self._events.outputs.emit,
            on_status=self._events.kwinStatus.emit,
            on_activated=self._senses.window_activated,
            on_target_changed=self._senses.target_changed,
            on_popups=self._senses.popups_changed,
        )

    # ---- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        self._view.show()  # the penguin first; everything else after the first frame
        self._tray.show()
        QTimer.singleShot(0, self._start_background)

    def _start_background(self) -> None:
        self._watcher.start()
        self._io.start()
        self._io.submit(self._kwin.start())
        self._io.submit(self._start_sensors())
        if self._demo is None:
            self._io.submit(self._start_assist())
        if self._demo is not None and self._may_volunteer("demo-greeting"):
            self._demo.greet()
        log.event("desktop.started", demo=self._demo is not None)

    async def _start_sensors(self) -> None:
        try:
            await self._session.start()
        except Exception as exc:  # no session bus: the gate stays closed (lock unknown)
            log.warning("session.start_failed", error=type(exc).__name__)
        try:
            plan = await make_plan(self._config.hotkeys)
            result = await bind(plan, self._hotkeys)
            self._events.hotkeysBound.emit(shortcut_labels(plan, result))
        except Exception as exc:
            log.warning("hotkeys.start_failed", error=type(exc).__name__)

    def _eyes_from_io(self, state: EyesState) -> None:
        """Called by the senses on the I/O thread."""
        self._events.eyes.emit(state)
        self._relay.identity_changed()  # a different window may need a different question

    def _build_assist(self) -> Assist:
        relay = self._relay
        assist = build_assist(
            self._paths,
            self._senses,
            client_version=_client_version(),
            cache=self._identity_cache,
            on_delta=relay.delta,
            on_done=relay.done,
            on_error=relay.error,
            on_link_state=self._events.linkState.emit,
            on_identity=relay.identity_changed,
        )
        relay.assist = assist
        return assist

    async def _start_assist(self) -> None:
        """On the I/O loop, after the first frame: wire the brain link and start reaching."""
        try:
            assist = self._build_assist()  # reads the app packs from disk
            assist.start()
        except Exception as exc:  # Waddle still runs; asking says he is not ready
            log.warning("assist.start_failed", error=type(exc).__name__)
            self._assist_failed = True  # "still starting up" would never become true
            return
        self._assist = assist
        self._relay.identity_changed()

    async def _ask_on_loop(self, assist: Assist, turn: int, text: str) -> None:
        # No await between these two: from here on every callback belongs to this turn (the
        # asker drops the callbacks of the question it replaces).
        self._relay.turn = turn
        try:
            await assist.asker.ask(text)
        except Exception as exc:
            log.warning("ask_failed", kind=type(exc).__name__)
            self._events.answerError.emit(turn, "internal")

    async def _stop_sensors(self) -> None:
        stops = [self._hotkeys.stop, self._session.stop, self._senses.stop, self._kwin.stop]
        if self._assist is not None:
            stops.insert(0, self._assist.stop)  # the open question and the link go first
        for stop in stops:
            try:
                await asyncio.wait_for(stop(), 1.5)
            except Exception as exc:
                log.warning("stop_failed", error=type(exc).__name__)

    def shutdown(self) -> None:
        self._watcher.stop()
        self._view.teardown()
        self._bridge.set_bubble("")  # ends a streaming answer: late events find nothing open
        for view in (self._ask, self._eyes_view):
            if view is not None:
                view.hide()
                view.setSource(QUrl())
        if self._io.running:
            try:
                self._io.submit(self._stop_sensors()).result(timeout=9.0)
            except Exception:
                log.warning("sensors.stop_failed")
            self._io.stop()
        self._frames.set_frame(None)
        self._identity_cache.close()
        self._save_timer.stop()
        self._flush_position()
        self._positions.close()
        self._tray.hide()

    # ---- theme and moods -------------------------------------------------------------

    def _on_palette(self, palette: ThemePalette) -> None:
        self._theme.apply(palette)
        self._tray.set_accent(palette.accent)
        self._bridge.set_view(motion_scale=palette.animation_factor)

    def _on_mood(self) -> None:
        mood = self._bridge.current_mood
        if not mood.is_transient:
            self._steady = mood
        elif self._bridge.property("motionScale") == 0:
            # Reduced motion: one-shot moods have no animation to finish, so time them out.
            QTimer.singleShot(1200, lambda: self._bridge.set_mood(self._steady))

    def _set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._io.call(self._senses.set_paused, paused)
        self._sync_presence()

    def _set_quiet(self, quiet: bool) -> None:
        self._quiet = quiet
        log.event("quiet", on=quiet)

    def _set_user_visible(self, visible: bool) -> None:
        self._user_visible = visible
        self._sync_presence()

    # ---- senses --------------------------------------------------------------------------

    def _on_eyes(self, state: EyesState) -> None:
        self._eyes_state = state
        self._sensors_reported = True
        self._eyes_model.set_state(state)
        self._sync_presence()

    def _on_frame(self, frame: Frame | None) -> None:
        visible = self._eyes_view is not None and self._eyes_view.isVisible()
        # A frame is kept only while the panel that shows it is open.
        self._eyes_model.set_frame(frame if visible else None)

    def _sync_presence(self) -> None:
        """Mood, visibility and badge follow what the senses and the user say."""
        state = self._eyes_state
        if self._paused or state.blind is BlindReason.LOCKED:
            base = Mood.SLEEPING
        elif state.eyes_closed and self._sensors_reported:
            base = Mood.EYES_CLOSED
        elif self._brain_down:
            base = Mood.NAPPING_BRAIN  # also mid-answer: nobody is thinking any more
        elif self._asking:
            base = Mood.THINKING
        else:
            base = Mood.IDLE
        if base is not self._base_mood:
            self._base_mood = base
            self._bridge.set_mood(base)
        if self._paused:
            self._tray.set_state(TrayState.PAUSED)
        else:
            self._tray.set_state(TrayState.BRAIN_DOWN if self._brain_down else TrayState.WATCHING)

        fullscreen = state.window is not None and state.window.fullscreen
        if not fullscreen:
            self._brought_back = False
        mode = self._config.watching.fullscreen if fullscreen and not self._brought_back else ""
        self._bridge.set_badge(mode == "badge")
        want_visible = self._user_visible and mode != "hide"
        if self._view.isVisible() != want_visible:
            self._view.setVisible(want_visible)

    def _on_hotkey(self, action: str, pressed: bool) -> None:
        if not pressed:
            return
        log.event("hotkey", action=action)
        if action == "ask":
            self.open_ask()
        elif action == "pause":
            self._tray.pause.toggle()
        elif action == "quiet":
            self._tray.quiet.toggle()
        elif action == "bring_back":
            self._bring_back()

    def _bring_back(self) -> None:
        """Show him again: over a fullscreen app, or after "Show Waddle" was switched off."""
        self._brought_back = True
        if self._tray.show_waddle.isChecked():
            self._sync_presence()
        else:
            self._tray.show_waddle.setChecked(True)

    def _may_volunteer(self, category: str) -> bool:
        """The proactivity gate: every bubble nobody asked for goes through here."""
        state, now = self._eyes_state, _now_ms()
        context = Context(
            dnd=state.session.dnd,
            quiet=self._quiet,
            focus_mode=False,
            user_idle_ms=0,
            fullscreen=state.window is not None and state.window.fullscreen,
            locked=state.session.locked,
            paused=self._paused,
        )
        decision = decide(Kind.TIP, category, context, self._tips, now, min_idle_ms=0)
        log.event("proactive", category=category, decision=decision.value)
        if decision not in (Decision.SHOW, Decision.SHOW_SILENT):
            return False
        self._tips.record_shown(category, now)
        return True

    # ---- Waddle's eyes -------------------------------------------------------------------

    def open_eyes(self) -> None:
        if self._eyes_view is None:
            view = QQuickView()
            view.setTitle("Waddle's eyes")
            view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
            view.engine().addImageProvider("eyes", self._frames)
            view.setInitialProperties({"theme": self._theme, "eyes": self._eyes_model})
            view.setSource(QUrl.fromLocalFile(str(QML_DIR / "Eyes.qml")))
            view.visibleChanged.connect(self._on_eyes_visible)
            self._eyes_view = view
        self._eyes_view.show()
        self._eyes_view.requestActivate()

    def _on_eyes_visible(self, visible: bool) -> None:
        self._io.call(self._senses.set_watching, visible)
        if not visible:
            self._eyes_model.set_frame(None)

    def _look_now(self) -> None:
        if self._io.running:
            self._io.submit(self._senses.look(force=True))

    # ---- pointer on the penguin ------------------------------------------------------

    def _on_hover(self, inside: bool) -> None:
        self._dodge_event("hover_enter" if inside else "hover_leave")

    def _dodge_event(self, name: str) -> None:
        getattr(self._dodge, name)(_now_ms())
        self._dodge_sync()

    def _dodge_tick(self) -> None:
        self._dodge.tick(_now_ms())
        self._dodge_sync()

    def _sync_blocked(self) -> None:
        self._dodge.set_blocked(self._bridge.overlay_open, _now_ms())
        self._dodge_sync()

    def _dodge_sync(self) -> None:
        self._bridge.set_dodged(self._dodge.dodged)
        deadline = self._dodge.deadline
        if deadline is None:
            self._dodge_timer.stop()
        else:
            self._dodge_timer.start(max(0, deadline - _now_ms()))

    def _on_click(self) -> None:
        b = self._bridge
        if b.guide_view is not None:
            b.set_card_open(not bool(b.property("cardOpen")))
        elif b.property("bubbleText"):
            b.dismissBubble()  # also stops an answer that is still arriving
        elif self._demo is not None:
            self._demo.toggle_guide()

    # ---- the brain -------------------------------------------------------------------
    #
    # UNVERIFIED (S3.2, and the Phase 3 "done when" checks on a real desktop): everything
    # below was run offscreen against a fake brain on a real Unix socket. Not run: a real
    # brain and model, a real Plasma session (the Ask window growing for its version hint,
    # Stop and the version button inside the penguin window's input mask), first-words timing.

    def _on_link_state(self, state: LinkState) -> None:
        self._link = state
        if state.status == "up":
            self._brain_down = False
            if self._asleep_shown and self._bridge.property("bubbleText") == self._asleep_shown:
                self._bridge.set_bubble("")  # no longer true
            self._asleep_shown = ""
        elif state.status == "down" and state.reason != "stopped":
            self._brain_down = True
        # "connecting" is one more try: he stays as he was, so a brain that is away does not
        # make him blink awake at every attempt. "stopped" is Waddle's own doing.
        self._sync_presence()

    def _ask_question(self, text: str) -> None:
        """You asked something. Always allowed: the proactivity gate is for the unasked."""
        bridge = self._bridge
        self._turn += 1
        self._bubble_version = None
        bridge.set_card_open(False)  # the answer goes in the bubble, which the card hides
        if not self._view.isVisible():
            self._bring_back()  # you asked: the answer needs somewhere to be seen
        assist = self._assist
        if assist is None:
            bridge.set_bubble(START_FAILED if self._assist_failed else STARTING)
            return
        up = self._link is not None and self._link.status == "up"
        # While the brain is away nothing is promised: the asker answers "brain_down" at once
        # (and tries the brain again), unless it came back this very moment.
        bridge.begin_stream(THINKING if up else "")
        self._asking = up
        self._sync_presence()
        log.event("ui_ask", brain=self._link.status if self._link is not None else "unknown")
        self._io.submit(self._ask_on_loop(assist, self._turn, text))

    def _live(self, turn: int) -> bool:
        """The event belongs to the question whose answer is still open in the bubble."""
        return turn == self._turn and self._bridge.streaming

    def _answer_over(self) -> None:
        self._asking = False
        self._sync_presence()

    def _on_answer_delta(self, turn: int, text: str) -> None:
        if self._live(turn):
            # Cleaned where it was received too; this is the last stop before the screen.
            self._bridge.push_delta(clean(text, MAX_DELTA_CHARS))

    def _on_answer_done(self, turn: int, sources: object, ask: object) -> None:
        if not self._live(turn):
            return
        lines = [source_line(sources if isinstance(sources, tuple) else ())]
        action = ""
        if isinstance(ask, VersionAsk) and ask.key not in self._version_asked:
            # Once per app per session. After that the tray entry is the way in.
            self._version_asked.add(ask.key)
            self._bubble_version = ask
            lines.append(f"I can't tell which version of {ask.name} this is.")
            action = VERSION_BUTTON
        note = "\n".join(line for line in lines if line)
        self._bridge.end_stream(note=note, action=action, fallback=NO_ANSWER)
        self._answer_over()

    def _on_answer_error(self, turn: int, code: str) -> None:
        if not self._live(turn):
            return
        bridge = self._bridge
        if code == "brain_down":
            self._asleep_shown = asleep_text(self._link)
            bridge.set_bubble(self._asleep_shown)
        else:
            waking = self._link is not None and self._link.waking is not None
            sentence = error_text(code, waking=waking)
            if bridge.stream_has_text:
                bridge.end_stream(note=sentence)  # what arrived stays, with why it stopped
            else:
                bridge.end_stream(fallback=sentence)
        self._answer_over()

    def _on_stop(self) -> None:
        """Stop was clicked, or the bubble was closed mid-answer."""
        assist = self._assist
        if assist is not None:
            self._io.call(assist.asker.cancel)
        self._bridge.end_stream()  # what has arrived stays
        self._answer_over()

    # ---- "which version is this?" ----------------------------------------------------

    def _on_identity(self, ask: object) -> None:
        self._version_ask = ask if isinstance(ask, VersionAsk) else None
        self._tray.set_version_wanted(self._version_ask is not None)

    def _on_bubble_action(self) -> None:
        if self._bubble_version is not None:
            self._show_ask(self._bubble_version)

    def open_version(self) -> None:
        """You chose "Tell Waddle the version" yourself (tray)."""
        if self._version_ask is not None:
            self._show_ask(self._version_ask)

    def _on_version_applied(self, outcome: str, label: str) -> None:
        asked, self._version_replied = self._version_replied, None
        if self._bridge.streaming:
            self._on_stop()
        log.event("version_told", outcome=outcome)
        if outcome == "ok":
            self._bridge.set_bubble(f"Got it: {label}." if label else "Got it. Thank you.")
        elif outcome == "unreadable":
            self._bubble_version = asked
            action = VERSION_BUTTON if asked is not None else ""
            self._bridge.set_bubble(VERSION_UNREADABLE, action=action)
        else:
            # While paused nothing is known about the app in front: "click on it" would not help.
            self._bridge.set_bubble(VERSION_PAUSED if self._paused else VERSION_MOVED)

    # ---- the Ask window --------------------------------------------------------------

    def open_ask(self) -> None:
        self._show_ask(None)
        assist = self._assist
        if assist is not None:
            # Have the answering model loaded by the time you finish typing. While the brain
            # is away this tries it again instead.
            self._io.call(assist.asker.prewarm)

    def _show_ask(self, version: VersionAsk | None) -> None:
        """The text window: for a question, or (``version``) for one app's version."""
        if self._ask is None:
            ask = QQuickView()
            ask.setTitle("Ask Waddle")
            ask.setColor(QColor(Qt.GlobalColor.transparent))
            ask.setFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
            ask.setResizeMode(QQuickView.ResizeMode.SizeViewToRootObject)
            ask.setInitialProperties({"theme": self._theme})
            ask.setSource(QUrl.fromLocalFile(str(QML_DIR / "Ask.qml")))
            root = ask.rootObject()
            assert root is not None
            root.submitted.connect(self._on_ask_submitted)  # type: ignore[attr-defined]
            root.cancelled.connect(ask.hide)  # type: ignore[attr-defined]
            self._ask = ask
        root = self._ask.rootObject()
        assert root is not None
        if version != self._ask_version:
            root.clearInput()  # type: ignore[attr-defined]  # half a question is not a version
        self._ask_version = version
        root.setProperty("hint", version_hint(version) if version is not None else "")
        root.setProperty("placeholder", VERSION_PLACEHOLDER if version else ASK_PLACEHOLDER)
        self._ask.show()
        self._ask.requestActivate()
        root.focusInput()  # type: ignore[attr-defined]

    def _on_ask_submitted(self, text: str) -> None:
        if self._ask is not None:
            self._ask.hide()
        version, self._ask_version = self._ask_version, None
        if version is None:
            self._bridge.submitText(text)
        elif self._assist is not None:
            self._version_replied = version
            self._io.call(self._relay.apply_version, version.key, text[:_MAX_VERSION_TEXT])

    # ---- placement (KWin) ------------------------------------------------------------

    def _on_kwin_status(self, ok: bool, reason: str) -> None:
        log.event("kwin.status", ok=ok, reason=reason.replace(" ", "_"))

    def _on_outputs(self, outputs: list[OutputInfo], active: str | None) -> None:
        self._outputs = {o.name: Rect(o.x, o.y, o.width, o.height) for o in outputs}
        self._place_if_needed()

    def _on_own_window(self, own: OwnWindow) -> None:
        if own.title != PENGUIN_WINDOW_TITLE:
            return
        self._last_own = own
        if own.uuid != self._own_uuid:
            # A new surface (first start, re-shown after Hide, KWin restarted): KWin chose
            # where it is, not the user. Put it back rather than remembering that spot.
            self._own_uuid = own.uuid
            self._placed = False
        if not self._placed:
            self._place_if_needed()
            return
        if (own.x, own.y) == self._move_target:
            self._move_target = None  # the echo of our own move, not a drag
            return
        output = self._outputs.get(own.output)
        if output is not None:  # the user moved him: remember it for this monitor
            # KWin reports every step of a drag; write once, after it settles.
            self._pending_save = (output, own.output, (own.x, own.y))
            self._save_timer.start()

    def _flush_position(self) -> None:
        if self._pending_save is not None:
            output, name, pos = self._pending_save
            self._pending_save = None
            self._positions.save(output, name, pos)

    def _place_if_needed(self) -> None:
        """Once per surface: restore the saved spot for this monitor, else bottom-right."""
        own = self._last_own
        if self._placed or own is None or own.output not in self._outputs:
            return
        self._placed = True
        output, size = self._outputs[own.output], (own.width, own.height)
        saved = self._positions.load(output, own.output)
        target = (
            clamp_to_output(saved, output, size)
            if saved is not None
            else default_position(output, size, self._config.placement.margin_px)
        )
        if target != (own.x, own.y):
            self._move_target = target
            self._io.submit(self._kwin.move_own_window(PENGUIN_WINDOW_TITLE, *target))


def _on_uncaught(
    kind: type[BaseException], value: BaseException, _traceback: TracebackType | None
) -> None:
    """An error nobody caught on the UI thread (a slot, a timer).

    Python would print the traceback with the error's text, and that text can quote an answer
    or an app's name. The log gets the kind of error and where it was raised.
    """
    log.warning("ui_error", kind=kind.__name__, where=error_site(value))


def install_error_hook() -> None:
    sys.excepthook = _on_uncaught


def _install_signal_handlers(app: QApplication) -> QSocketNotifier:
    """Quit cleanly on SIGINT/SIGTERM without a polling timer (idle CPU stays at zero)."""
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    signal.set_wakeup_fd(writer.fileno())
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: None)
    notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read, app)
    notifier.activated.connect(app.quit)
    notifier._sockets = (reader, writer)  # type: ignore[attr-defined]  # keep them alive
    return notifier


def acquire_instance_lock(paths: WaddlePaths) -> int | None:
    """Take the single-instance lock and record our pid. None if another Waddle holds it.

    The lock (not the file's existence or contents) is what proves a Waddle is running, so a
    stale file left by a crash can never make ``waddle stop`` signal an unrelated process.
    """
    fd = os.open(paths.pid_file, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def run(config: WaddleConfig, paths: WaddlePaths, *, demo: bool = False) -> int:
    for directory in paths.all_dirs():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = acquire_instance_lock(paths)
    if lock is None:
        sys.stdout.write("Waddle is already running.\n")
        return 0

    fmt = QSurfaceFormat.defaultFormat()
    fmt.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Waddle")
    app.setDesktopFileName(APP_ID)  # becomes the Wayland app id / window class
    app.setQuitOnLastWindowClosed(False)
    _install_signal_handlers(app)

    install_error_hook()
    waddle = WaddleApp(config, paths, demo=demo)
    waddle.start()
    try:
        return app.exec()
    finally:
        waddle.shutdown()
        paths.pid_file.unlink(missing_ok=True)  # while still holding the lock
        os.close(lock)
