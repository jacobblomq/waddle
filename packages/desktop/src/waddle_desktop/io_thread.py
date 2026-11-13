"""The I/O thread: one asyncio loop for D-Bus, sockets and subprocesses (D-003).

The UI thread never blocks on I/O. Results come back as queued Qt signals on ``HostEvents``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from waddle_desktop import log


def error_site(exc: BaseException | None) -> str:
    """Where an error was raised, as "module:line". Never its text, which may quote data."""
    tb = exc.__traceback__ if exc is not None else None
    if tb is None:
        return "unknown"
    while tb.tb_next is not None:
        tb = tb.tb_next
    return f"{Path(tb.tb_frame.f_code.co_filename).stem}:{tb.tb_lineno}"


def _on_loop_error(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """An error nobody caught on the I/O loop (a callback, or a task nobody awaited).

    asyncio would print the traceback with the error's text, and that text can quote a window
    title, a path or an answer. The log gets the kind of error and where it was raised.
    """
    exc = context.get("exception")
    kind = type(exc).__name__ if isinstance(exc, BaseException) else "none"
    log.warning("io_error", kind=kind, where=error_site(exc))


class HostEvents(QObject):
    """Signals emitted from the I/O thread; Qt delivers them queued on the UI thread."""

    ownWindow = Signal(object)
    outputs = Signal(object, object)  # list[OutputInfo], active output name | None
    kwinStatus = Signal(bool, str)
    eyes = Signal(object)  # EyesState
    frame = Signal(object)  # Frame | None
    hotkey = Signal(str, bool)  # action id, pressed
    hotkeysBound = Signal(object)  # dict: action id -> key as text ("" if none)
    # The brain and the answer. ``turn`` is the UI's number for the question an event belongs
    # to, so an event of an older question that was still queued can be told apart.
    linkState = Signal(object)  # LinkState
    answerDelta = Signal(int, str)  # turn, cleaned text (show as plain text only)
    answerDone = Signal(int, object, object)  # turn, source labels, VersionAsk | None
    answerError = Signal(int, str)  # turn, error code
    identity = Signal(object)  # VersionAsk | None: the app in front needs you to say its version
    versionApplied = Signal(str, str)  # "ok" | "unreadable" | "moved", then "App 1.2"


class IoThread:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.set_exception_handler(_on_loop_error)
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
