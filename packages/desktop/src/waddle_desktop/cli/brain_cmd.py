"""``waddle brain up|down|status|logs``: Waddle's own brain, through ``BrainCtl`` only.

Nothing here starts a process itself. Every action is one of the fixed operations of
``waddle_desktop.brainctl.compose.BrainCtl``, which blocks; that is fine in the CLI process.
Exit status: 0 done, 1 failed, 2 could not be tried (usage, Docker or a file is missing).
"""

from __future__ import annotations

import contextlib
import signal
import sys
import time
from collections.abc import Callable, Iterator
from typing import Final, Protocol

from waddle_core.link import Role
from waddle_core.paths import WaddlePaths
from waddle_desktop.brainctl.compose import SERVICES, BrainCtl, Logs, Outcome, Result, Status
from waddle_desktop.link.token import TokenError, ensure_token

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_UNAVAILABLE: Final = 2
NATIVE_POLL_SECONDS: Final = 0.25
DEFAULT_LOG_TAIL: Final = 200

NATIVE_NOTICE: Final = """\
Native mode runs the brain as an ordinary program instead of in a container.
What that gives up:
  - The network boundary: the brain has your normal internet access.
  - The file boundary: the brain can read whatever you can read.
  - Control of the model server: it uses the Ollama you already run on
    127.0.0.1:11434. Waddle does not start, stop or configure it, and
    `waddle models pull` does not download into it.
The private socket and the link token stay the same."""


class Ctl(Protocol):
    """The part of ``BrainCtl`` the CLI uses; tests pass a stand-in."""

    def up(self) -> Result: ...
    def down(self) -> Result: ...
    def status(self) -> Status: ...
    def logs(self, tail: int = ...) -> Logs: ...
    def pull_model(self, role: Role, *, progress: bool = ...) -> Result: ...
    def start_native(self) -> Result: ...
    def stop_native(self) -> Result: ...
    def native_running(self) -> bool: ...


def make_ctl(paths: WaddlePaths) -> Ctl:
    return BrainCtl(paths)


def exit_code(result: Result) -> int:
    return {Outcome.OK: EXIT_OK, Outcome.FAILED: EXIT_FAILED}.get(result.outcome, EXIT_UNAVAILABLE)


def _problem(result: Result, hint: str = "") -> int:
    print(result.reason or "That did not work.", file=sys.stderr)
    if hint:
        print(hint, file=sys.stderr)
    return exit_code(result)


def _token_ready(paths: WaddlePaths) -> bool:
    """Make sure the link token exists. Its value is never printed."""
    try:
        ensure_token(paths.link_token)
    except TokenError:
        print("Waddle's link token could not be created or is not safe to use.", file=sys.stderr)
        print(
            f"It must be a plain file only you can read: {paths.link_token}\n"
            "Remove it and try again to get a new one.",
            file=sys.stderr,
        )
        return False
    return True


def up(paths: WaddlePaths, ctl: Ctl) -> int:
    if not _token_ready(paths):
        return EXIT_UNAVAILABLE
    print("Starting Waddle's brain. The first time, Docker downloads and builds it:")
    print("that can take several minutes and prints nothing until it is done.")
    result = ctl.up()
    if not result.ok:
        return _problem(result, "See what is missing with: waddle doctor")
    print("The brain's containers are started.")
    print("Check them:       waddle brain status")
    print("Download models:  waddle models pull all")
    return EXIT_OK


class _Stop:
    """Set by SIGINT, SIGTERM or SIGHUP while the native brain runs in the foreground."""

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, _signum: int, _frame: object) -> None:
        self.requested = True


@contextlib.contextmanager
def _stop_signals() -> Iterator[_Stop]:
    """Turn the usual "please stop" signals into a flag, so the child is always stopped.

    The handlers are Python functions, so the child does not inherit them (handlers are
    reset when a program is started); it still gets its own SIGTERM from ``stop_native``.
    """
    stop = _Stop()
    old: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            old[signum] = signal.signal(signum, stop)
        except ValueError:  # not the main thread: Ctrl-C still arrives as KeyboardInterrupt
            break
    try:
        yield stop
    finally:
        for number, handler in old.items():
            signal.signal(number, handler)  # type: ignore[arg-type]


def up_native(paths: WaddlePaths, ctl: Ctl, sleep: Callable[[float], None] = time.sleep) -> int:
    """Run the brain as a child of this command until Ctrl-C or until it exits by itself."""
    print(NATIVE_NOTICE)
    print()
    if not _token_ready(paths):
        return EXIT_UNAVAILABLE
    asked_to_stop = False
    try:
        with _stop_signals() as stop:
            result = ctl.start_native()
            if not result.ok:
                return _problem(result, "Its own messages, if any, are above.")
            print("The brain is running. Keep this terminal open; press Ctrl-C to stop it.")
            try:
                while not stop.requested and ctl.native_running():
                    sleep(NATIVE_POLL_SECONDS)
            except KeyboardInterrupt:
                stop.requested = True
            asked_to_stop = stop.requested
    except KeyboardInterrupt:
        asked_to_stop = True
    finally:
        ctl.stop_native()  # always: this command must not leave the brain behind
    if asked_to_stop:
        print("\nThe brain is stopped.")
        return EXIT_OK
    print("The brain stopped by itself. Its own messages, if any, are above.", file=sys.stderr)
    print("Is Ollama running on 127.0.0.1:11434? Is the container brain still up?", file=sys.stderr)
    return EXIT_FAILED


def down(ctl: Ctl) -> int:
    result = ctl.down()
    if not result.ok:
        return _problem(result)
    print("The brain is stopped. Downloaded models are kept.")
    return EXIT_OK


def status(ctl: Ctl) -> int:
    result = ctl.status()
    if not result.ok:
        return _problem(result, "See what is missing with: waddle doctor")
    by_name = {service.service: service for service in result.services}
    all_running = True
    for name in SERVICES:
        service = by_name.get(name)
        if service is None:
            print(f"{name}: not started")
            all_running = False
            continue
        health = f" ({service.health})" if service.health else ""
        print(f"{name}: {service.state}{health}")
        all_running = all_running and service.state == "running"
    if not all_running:
        print("Start the brain with: waddle brain up")
        return EXIT_FAILED
    return EXIT_OK


def logs(ctl: Ctl, tail: int) -> int:
    result = ctl.logs(tail)
    if not result.ok:
        return _problem(result)
    # Container output: BrainCtl has already removed control characters and capped it.
    print(result.text.rstrip("\n") or "The brain's containers have printed nothing yet.")
    return EXIT_OK
