"""Waddle's eyes: shows exactly what Waddle last saw and what he thinks he is looking at.

The frame is two encoded images held in memory by ``FrameProvider`` for as long as the panel
is open and the gate allows. Nothing here writes them anywhere.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import Property, QObject, QSize, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from waddle_core.privacy import BlindReason
from waddle_desktop.sensors.capture_client import Frame
from waddle_desktop.sensors.senses import EyesState, Look

BLIND_TEXT: dict[BlindReason, str] = {
    BlindReason.LOCKED: "No — the screen is locked",
    BlindReason.UNKNOWN_LOCK: "No — I can't tell whether the screen is locked",
    BlindReason.PAUSED: "No — I'm paused",
    BlindReason.BLOCKED: "No — this window is on the private list",
    BlindReason.OWN_WINDOW: "No — that's my own window",
    BlindReason.NO_WINDOW: "Nothing to look at — no app window is focused",
}

LOOK_HINTS: dict[str, str] = {
    "not_authorized": "KWin refused the capture. Run ./install.sh again, then log out and in.",
    "no_capture_path": "KWin's screenshot service is not available in this session.",
    "config_unreadable": "config.toml cannot be read, so the private list is unknown. Fix it.",
    "lock_unknown": "The lock state cannot be read (logind did not answer).",
    "helper-missing": "The capture helper is not installed. Run ./install.sh (needs cargo).",
    "helper-unsafe_permissions": "The capture helper file has unsafe permissions.",
    "helper-exited": "The capture helper keeps exiting. Run: waddle doctor",
    "timeout": "KWin did not answer in time.",
}


def _look_text(look: Look | None, now: float) -> str:
    if look is None:
        return "not yet"
    age = max(0, round(now - look.at))
    when = "just now" if age < 2 else f"{age} s ago"
    if look.status == "ok":
        popups = f", {look.popups} popup(s)" if look.popups else ""
        return f"{look.width} x {look.height} px in {look.elapsed_ms} ms{popups}, {when}"
    if look.status == "unchanged":
        return f"nothing changed, {when}"
    hint = LOOK_HINTS.get(look.detail, "")
    return f"{look.status}: {look.detail}. {hint}".strip()


def eyes_rows(state: EyesState, now: float | None = None) -> list[dict[str, str]]:
    """The panel's text, as label/value rows. Window titles are deliberately not shown."""
    now = time.time() if now is None else now
    rows: list[tuple[str, str]] = []
    rows.append(("May I look?", "Yes" if state.blind is None else BLIND_TEXT[state.blind]))
    window, identity = state.window, state.identity
    if window is None:
        rows.append(("App", "—"))
    else:
        if identity is None:
            app = "working it out…"
        else:
            name = identity.name or "unknown app"
            version = str(identity.version) if identity.version else "version unknown"
            app = f"{name} · {version} · {identity.packaging.value}"
        rows.append(("App", app))
        if state.identify_ms is not None:
            rows.append(("Named in", f"{state.identify_ms:.0f} ms (budget 100 ms)"))
        rows.append(("Window class", window.resource_class or "—"))
        kind = {True: "XWayland", False: "Wayland", None: "unknown"}[window.xwayland]
        size = f"{window.frame.width:.0f} x {window.frame.height:.0f} at {window.scale:g}x"
        rows.append(("Window", f"{size}, {kind}{', fullscreen' if window.fullscreen else ''}"))
    helper = state.helper
    if helper is None:
        rows.append(("Capture path", "helper not started yet"))
    elif helper.screenshot2_version is None:
        rows.append(("Capture path", "none: KWin ScreenShot2 is not available"))
    else:
        regime = {
            "restricted": "authorized by the helper's desktop file",
            "open": "no authorization needed on this Plasma",
            "unknown": "authorization rule unknown",
        }[helper.permission_regime]
        rows.append(("Capture path", f"KWin ScreenShot2 v{helper.screenshot2_version}, {regime}"))
    rows.append(("Last look", _look_text(state.look, now)))
    session = state.session
    lock = "locked" if session.locked else ("unlocked" if session.lock_known else "unknown")
    rows.append(("Session", f"{lock}, Do Not Disturb {'on' if session.dnd else 'off'}"))
    return [{"label": label, "value": value} for label, value in rows]


class FrameProvider(QQuickImageProvider):
    """Serves the last overview and crop to QML from memory (``image://eyes/overview``)."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._lock = threading.Lock()
        self._images: dict[str, bytes] = {}

    def set_frame(self, frame: Frame | None) -> None:
        with self._lock:
            self._images = {} if frame is None else {"overview": frame.overview, "crop": frame.crop}

    @property
    def has_frame(self) -> bool:
        with self._lock:
            return bool(self._images)

    def requestImage(self, id: str, size: QSize, requestedSize: QSize) -> QImage:
        with self._lock:
            data = self._images.get(id.split("?", 1)[0])
        image = QImage.fromData(data) if data else QImage()
        if image.isNull():
            image = QImage(1, 1, QImage.Format.Format_ARGB32)
            image.fill(0)
        size.setWidth(image.width())
        size.setHeight(image.height())
        return image


class EyesModel(QObject):
    changed = Signal()
    frameChanged = Signal()
    lookRequested = Signal()

    def __init__(self, provider: FrameProvider, parent: QObject | None = None):
        super().__init__(parent)
        self._provider = provider
        self._state = EyesState()
        self._revision = 0

    def set_state(self, state: EyesState) -> None:
        self._state = state
        self.changed.emit()

    def set_frame(self, frame: Frame | None) -> None:
        self._provider.set_frame(frame)
        self._revision += 1
        self.frameChanged.emit()

    rows = Property("QVariantList", lambda self: eyes_rows(self._state), notify=changed)  # type: ignore[arg-type]
    eyesClosed = Property(bool, lambda self: self._state.eyes_closed, notify=changed)
    canLook = Property(bool, lambda self: self._state.blind is None, notify=changed)
    hasFrame = Property(bool, lambda self: self._provider.has_frame, notify=frameChanged)
    revision = Property(int, lambda self: self._revision, notify=frameChanged)

    @Slot()
    def lookNow(self) -> None:
        self.lookRequested.emit()
