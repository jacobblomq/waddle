"""QML-facing view of a ThemePalette. QML binds to these properties; one signal covers all."""

from __future__ import annotations

from PySide6.QtCore import Property, QObject, Signal

from waddle_desktop.theme.palette import FALLBACK_LIGHT, ThemePalette


def _prop(name: str, kind: type, notify: Signal) -> Property:
    def getter(self: ThemeObject) -> object:
        return getattr(self.palette, name)

    return Property(kind, getter, notify=notify)  # type: ignore[arg-type]


class ThemeObject(QObject):
    changed = Signal()

    def __init__(self, palette: ThemePalette = FALLBACK_LIGHT, parent: QObject | None = None):
        super().__init__(parent)
        self._p = palette

    def apply(self, palette: ThemePalette) -> None:
        if palette != self._p:
            self._p = palette
            self.changed.emit()

    @property
    def palette(self) -> ThemePalette:
        return self._p

    accent = _prop("accent", str, changed)
    windowBg = _prop("window_bg", str, changed)  # noqa: N815
    windowFg = _prop("window_fg", str, changed)  # noqa: N815
    viewBg = _prop("view_bg", str, changed)  # noqa: N815
    viewFg = _prop("view_fg", str, changed)  # noqa: N815
    buttonBg = _prop("button_bg", str, changed)  # noqa: N815
    buttonFg = _prop("button_fg", str, changed)  # noqa: N815
    highlight = _prop("highlight", str, changed)
    highlightedFg = _prop("highlighted_fg", str, changed)  # noqa: N815
    positive = _prop("positive", str, changed)
    negative = _prop("negative", str, changed)
    neutral = _prop("neutral", str, changed)
    link = _prop("link", str, changed)
    isDark = _prop("is_dark", bool, changed)  # noqa: N815
    fontFamily = _prop("font_family", str, changed)  # noqa: N815
    fontPointSize = _prop("font_point_size", float, changed)  # noqa: N815
