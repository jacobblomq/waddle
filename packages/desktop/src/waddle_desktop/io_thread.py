"""The I/O thread: one asyncio loop for D-Bus, sockets and subprocesses (D-003).

The UI thread never blocks on I/O. Results come back as queued Qt signals on ``HostEvents``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any

from PySide6.QtCore import QObject, Signal


class HostEvents(QObject):
    """Signals emitted from the I/O thread; Qt delivers them queued on the UI thread."""

    ownWindow = Signal(object)
    outputs = Signal(object, object)  # list[OutputInfo], active output name | None
    kwinStatus = Signal(bool, str)
    eyes = Signal(object)  # EyesState
    frame = Signal(object)  # Frame | None
    hotkey = Signal(str, bool)  # action id, pressed
    hotkeysBound = Signal(object)  # dict: action id -> key as text ("" if none)


class IoThread:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="waddle-io", daemon=True)
        self._started = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._started.set)
        self._loop.run_forever()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()
        self._started.wait()

    def submit[T](self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call(self, fn: Callable[..., object], *args: object) -> None:
        """Run a plain function on the I/O loop (for objects that live there)."""
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(fn, *args)

    def stop(self, timeout: float = 2.0) -> None:
        if not self._thread.is_alive():
            if not self._loop.is_closed():
                self._loop.close()
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
        if not self._thread.is_alive():
            self._loop.close()
