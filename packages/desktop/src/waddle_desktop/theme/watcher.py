"""Notice when the Plasma theme changes by watching the kdeglobals files. Read-only."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from waddle_desktop.theme.palette import ThemePalette

_Stamp = tuple[int, int, int] | None  # (inode, mtime_ns, size); None when the file is absent


def _stamp(path: Path) -> _Stamp:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


class ThemeWatcher(QObject):
    """Emits ``changed(ThemePalette)`` when a reload yields a different palette.

    KConfig saves by writing a temp file and renaming it over the original, which silently drops
    a file watch. So the parent directories are watched too (this also catches the file being
    created later), and every file is re-watched before each reload.
    """

    changed = Signal(object)

    def __init__(
        self,
        paths: Sequence[Path],
        load: Callable[[], ThemePalette],
        debounce_ms: int = 150,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._paths = [Path(p) for p in paths]
        self._load = load
        self._palette = load()
        self._stamps: list[_Stamp] = []
        self._running = False
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._reload)

    @property
    def palette(self) -> ThemePalette:
        return self._palette

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        directories = sorted({str(p.parent) for p in self._paths if p.parent.is_dir()})
        if directories:
            self._watcher.addPaths(directories)
        self._reload()  # also picks up anything that changed since __init__

    def stop(self) -> None:
        self._running = False
        self._timer.stop()
        watched = self._watcher.files() + self._watcher.directories()
        if watched:
            self._watcher.removePaths(watched)

    def _on_file_changed(self, _path: str) -> None:
        if self._running:
            self._timer.start()  # restarts the countdown: a burst becomes one reload

    def _on_directory_changed(self, _path: str) -> None:
        # ~/.config is busy; only react when one of our files was created, replaced or removed.
        if self._running and [_stamp(p) for p in self._paths] != self._stamps:
            self._timer.start()

    def _reload(self) -> None:
        if not self._running:
            return
        self._rewatch_files()
        palette = self._load()
        if palette != self._palette:
            self._palette = palette
            self.changed.emit(palette)

    def _rewatch_files(self) -> None:
        """Point the file watches at whatever inode each path names now."""
        stale = self._watcher.files()
        if stale:
            self._watcher.removePaths(stale)
        existing = [str(p) for p in self._paths if p.is_file()]
        if existing:
            self._watcher.addPaths(existing)
        self._stamps = [_stamp(p) for p in self._paths]
