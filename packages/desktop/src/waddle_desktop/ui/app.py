"""The desktop application: wires the bridge, windows, tray, theme and KWin link together."""

from __future__ import annotations

import fcntl
import os
import signal
import socket
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QSocketNotifier, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QRegion, QSurfaceFormat
from PySide6.QtQuick import QQuickView
from PySide6.QtWidgets import QApplication

import waddle_desktop
from waddle_core.config import WaddleConfig
from waddle_core.identity import APP_ID, PENGUIN_WINDOW_TITLE
from waddle_core.mood import Mood
from waddle_core.paths import WaddlePaths
from waddle_desktop import log
from waddle_desktop.io_thread import HostEvents, IoThread
from waddle_desktop.kwin.link import KWinLink
from waddle_desktop.kwin.scripts import core_script_path
from waddle_desktop.kwin.types import OutputInfo, OwnWindow
from waddle_desktop.placement.positions import (
    PositionStore,
    Rect,
    clamp_to_output,
    default_position,
)
from waddle_desktop.theme.kdeglobals import kdeglobals_paths, load_palette
from waddle_desktop.theme.palette import ThemePalette
from waddle_desktop.theme.watcher import ThemeWatcher
from waddle_desktop.ui.bridge import Bridge
from waddle_desktop.ui.demo import DemoDriver
from waddle_desktop.ui.dodge import DodgeMachine
from waddle_desktop.ui.theme_object import ThemeObject
from waddle_desktop.ui.tray import Tray, TrayState

_PACKAGE_DIR = Path(waddle_desktop.__file__).parent
QML_DIR = _PACKAGE_DIR / "qml"


def data_base() -> Path:
    """The directory holding ``kwin/``: packaged data in a wheel, the repo root from source."""
    packaged = _PACKAGE_DIR / "_data"
    return packaged if (packaged / "kwin").is_dir() else _PACKAGE_DIR.parents[3]


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


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
        self._steady = Mood.IDLE
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
            motion_scale=self._theme.palette.animation_factor,
            bubble_max_lines=config.ui.bubble_max_lines,
        )
        self._view = PenguinView(self._bridge)
        self._view.setSource(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
        self._ask: QQuickView | None = None

        self._tray = Tray(self._theme.palette.accent, demo=demo, parent=self)
        self._tray.pauseToggled.connect(self._set_paused)
        self._tray.visibilityToggled.connect(self._view.setVisible)
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

        self._positions = PositionStore(paths.desktop_db)
        self._events = HostEvents()
        self._events.ownWindow.connect(self._on_own_window)
        self._events.outputs.connect(self._on_outputs)
        self._events.kwinStatus.connect(self._on_kwin_status)
        self._io = IoThread()
        self._kwin = KWinLink(
            paths.runtime_dir,
            core_script_path(data_base()),
            on_own_window=self._events.ownWindow.emit,
            on_outputs=self._events.outputs.emit,
            on_status=self._events.kwinStatus.emit,
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
        if self._demo is not None:
            self._demo.greet()
        log.event("desktop.started", demo=self._demo is not None)

    def shutdown(self) -> None:
        self._watcher.stop()
        self._view.teardown()
        if self._ask is not None:
            self._ask.setSource(QUrl())
        if self._io.running:
            try:
                self._io.submit(self._kwin.stop()).result(timeout=2.0)
            except Exception:
                log.warning("kwin.stop_failed")
            self._io.stop()
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
        self._tray.set_state(TrayState.PAUSED if paused else TrayState.WATCHING)
        self._bridge.set_mood(Mood.SLEEPING if paused else Mood.IDLE)

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
            b.set_bubble("")
        elif self._demo is not None:
            self._demo.toggle_guide()

    # ---- the Ask window --------------------------------------------------------------

    def open_ask(self) -> None:
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
        self._ask.show()
        self._ask.requestActivate()
        root = self._ask.rootObject()
        assert root is not None
        root.focusInput()  # type: ignore[attr-defined]

    def _on_ask_submitted(self, text: str) -> None:
        if self._ask is not None:
            self._ask.hide()
        self._bridge.submitText(text)

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

    waddle = WaddleApp(config, paths, demo=demo)
    waddle.start()
    try:
        return app.exec()
    finally:
        waddle.shutdown()
        paths.pid_file.unlink(missing_ok=True)  # while still holding the lock
        os.close(lock)
