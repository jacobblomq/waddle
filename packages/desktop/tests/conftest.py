"""Headless Qt for tests: offscreen platform, software scene graph, no user config."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine

import waddle_desktop

QML_DIR = Path(waddle_desktop.__file__).parent / "qml"


@pytest.fixture
def qml_engine(qapp: Any) -> QQmlEngine:
    engine = QQmlEngine()
    engine.addImportPath(str(QML_DIR))
    return engine


@pytest.fixture
def load_qml(qml_engine: QQmlEngine) -> Callable[..., QObject]:
    """Create a component from ``qml/<name>`` with initial properties; fail on any QML error."""
    created: list[QObject] = []

    def _load(name: str, **props: Any) -> QObject:
        component = QQmlComponent(qml_engine, QUrl.fromLocalFile(str(QML_DIR / name)))
        assert component.isReady(), [e.toString() for e in component.errors()]
        obj = component.createWithInitialProperties(props)
        assert obj is not None, [e.toString() for e in component.errors()]
        created.append(obj)
        return obj

    yield _load  # type: ignore[misc]
    for obj in created:
        obj.deleteLater()
