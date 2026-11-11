"""The I/O thread: one asyncio loop for D-Bus, sockets and subprocesses (D-003).

The UI thread never blocks on I/O. Results come back as queued Qt signals on ``HostEvents``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any

from PySide6.QtCore import QObject, Signal


class HostEvents(QObject):
    """Signals emitted from the I/O thread; Qt delivers them queued on the UI thread."""

    ownWindow = Signal(object)  # noqa: N815  kwin.types.OwnWindow
    outputs = Signal(object, object)  # list[OutputInfo], active output name | None
    kwinStatus = Signal(bool, str)  # noqa: N815


class IoThread:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="waddle-io", daemon=True)
        self._started = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._started.set)
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._started.wait()

    def submit[T](self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 2.0) -> None:
        if not self._thread.is_alive():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
        if not self._thread.is_alive():
            self._loop.close()
