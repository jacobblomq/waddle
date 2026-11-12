"""System tray icon (StatusNotifierItem via QSystemTrayIcon). Icon is painted in code."""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal, SignalInstance
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayState(StrEnum):
    WATCHING = "watching"
    PAUSED = "paused"
    BRAIN_DOWN = "brain-down"


_DOT = {
    TrayState.WATCHING: "#27ae60",
    TrayState.PAUSED: "#7f8c8d",
    TrayState.BRAIN_DOWN: "#f67400",
}
_LABEL = {
    TrayState.WATCHING: "Waddle",
    TrayState.PAUSED: "Waddle — paused",
    TrayState.BRAIN_DOWN: "Waddle — brain unreachable",
}


def paint_icon(state: TrayState, accent: str, size: int = 64) -> QPixmap:
    """A small penguin head with a status dot; no image files involved."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    u = size / 64.0
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#2b3040"))
    p.drawEllipse(QRectF(8 * u, 4 * u, 48 * u, 54 * u))
    p.setBrush(QColor("#fbfbf6"))
    p.drawEllipse(QRectF(16 * u, 20 * u, 32 * u, 36 * u))
    p.setBrush(QColor("#1b1e28"))
    for cx in (25, 39):
        p.drawEllipse(QPointF(cx * u, 25 * u), 3.6 * u, 4.2 * u)
    beak = QPainterPath(QPointF(27 * u, 31 * u))
    beak.lineTo(37 * u, 31 * u)
    beak.lineTo(32 * u, 38 * u)
    beak.closeSubpath()
    p.setBrush(QColor("#f5a623"))
    p.drawPath(beak)
    p.setBrush(QColor(accent))
    p.drawRoundedRect(QRectF(12 * u, 44 * u, 40 * u, 9 * u), 4 * u, 4 * u)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(52 * u, 52 * u), 10 * u, 10 * u)
    p.setBrush(QColor(_DOT[state]))
    p.drawEllipse(QPointF(52 * u, 52 * u), 7.5 * u, 7.5 * u)
    p.end()
    return pix


class Tray(QObject):
    pauseToggled = Signal(bool)
    visibilityToggled = Signal(bool)
    quitRequested = Signal()
    demoRequested = Signal(str)
    quietToggled = Signal(bool)
    eyesRequested = Signal()

    def __init__(self, accent: str, *, demo: bool = False, parent: QObject | None = None):
        super().__init__(parent)
        self._accent = accent
        self._state = TrayState.WATCHING
        self._icon = QSystemTrayIcon(self)
        self._menu = QMenu()

        self.pause = self._check("Pause", self.pauseToggled)
        self.quiet = self._check("Quiet mode", self.quietToggled)
        # Shown disabled until the feature it controls exists, so nothing pretends to work.
        self.focus = self._later("Focus mode", "Phase 7")
        self._menu.addSeparator()
        self.show_waddle = self._check("Show Waddle", self.visibilityToggled, checked=True)
        # These arrive in later phases; shown disabled so the menu's shape is already final.
        self.eyes = self._menu.addAction("Waddle's eyes…")
        self.eyes.triggered.connect(self.eyesRequested)
        self.settings = self._later("Settings…", "Phase 9")
        if demo:
            self._menu.addSeparator()
            sub = self._menu.addMenu("Demo")
            for name in ("next mood", "say something", "toggle guide", "celebrate", "alert"):
                action = sub.addAction(name.capitalize())
                action.triggered.connect(lambda _=False, n=name: self.demoRequested.emit(n))
        self._menu.addSeparator()
        quit_action = self._menu.addAction("Quit")
        quit_action.triggered.connect(self.quitRequested)

        self._icon.setContextMenu(self._menu)
        self._refresh()

    def _check(self, text: str, signal: SignalInstance, *, checked: bool = False) -> QAction:
        action = self._menu.addAction(text)
        action.setCheckable(True)
        action.setChecked(checked)
        action.toggled.connect(signal)
        return action

    def _later(self, text: str, phase: str) -> QAction:
        action = self._menu.addAction(text)
        action.setEnabled(False)
        action.setToolTip(f"Arrives in {phase}")
        return action

    def _refresh(self) -> None:
        self._icon.setIcon(QIcon(paint_icon(self._state, self._accent)))
        self._icon.setToolTip(_LABEL[self._state])

    @property
    def state(self) -> TrayState:
        return self._state

    def set_state(self, state: TrayState) -> None:
        if state is not self._state:
            self._state = state
            self._refresh()

    def set_accent(self, accent: str) -> None:
        if accent != self._accent:
            self._accent = accent
            self._refresh()

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def action_texts(self) -> list[str]:
        return [a.text() for a in self._menu.actions() if not a.isSeparator()]
