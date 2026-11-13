"""``waddle models pull|status``: models by role, never by name (D-018).

The model behind each role is named in ``deploy/brain.toml`` and nowhere else. A pull goes
through ``BrainCtl.pull_model``; whether a model is present is asked of the running brain
over the private socket (one handshake, nothing else is sent).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from typing import Final, get_args

from waddle_core.link import ModelStatus, Role
from waddle_core.paths import WaddlePaths
from waddle_core.tools import ToolError
from waddle_desktop.brainctl.compose import ComposeError, configured_models
from waddle_desktop.cli.brain_cmd import EXIT_FAILED, EXIT_OK, EXIT_UNAVAILABLE, Ctl, exit_code
from waddle_desktop.link.client import BrainLink, LinkState
from waddle_desktop.link.tools_host import HostToolDispatcher

ROLES: Final[tuple[Role, ...]] = get_args(Role)
ALL: Final = "all"
PROBE_SECONDS: Final = 7.0  # a little over the link's own handshake timeout

# Why the brain could not be asked, in words. Keys are the link's reason codes.
_DOWN_REASONS: Final = {
    "no_socket": "the brain is not running",
    "unsafe_socket": "the brain's socket is not safe to use",
    "no_token": "the link token is missing or not private",
    "bad_token": "the brain refused Waddle's token",
    "busy": "the brain is in use by the running Waddle",
    "timeout": "the brain did not answer in time",
    "not_probed": "the brain could not be asked",
}

Configured = Callable[[], Mapping[Role, str]]


@dataclass(frozen=True)
class Probe:
    """What one handshake with the brain told us. ``models`` is empty unless ``up``."""

    up: bool
    reason: str = ""  # a link reason code when not up
    models: tuple[ModelStatus, ...] = ()

    def why(self) -> str:
        return _DOWN_REASONS.get(self.reason, "the brain did not answer")

    def model(self, role: str) -> ModelStatus | None:
        return next((m for m in self.models if m.role == role), None)


class _NoTools:
    """A probe asks nothing, so the brain has no reason to call a host tool."""

    async def app_state(self) -> dict[str, object]:
        raise ToolError("unavailable")

    async def system_stats(self, topic: str) -> dict[str, object]:
        raise ToolError("unavailable")

    async def man(self, name: str, section: str | None) -> dict[str, object]:
        raise ToolError("unavailable")

    async def tldr(self, name: str) -> dict[str, object]:
        raise ToolError("unavailable")


def _client_version() -> str:
    try:
        return metadata.version("waddle-desktop")[:32]
    except metadata.PackageNotFoundError:
        return "0"


async def probe_brain(paths: WaddlePaths, seconds: float = PROBE_SECONDS) -> Probe:
    """Connect once, read the brain's welcome, and leave. Never raises."""
    first: asyncio.Future[LinkState] = asyncio.get_running_loop().create_future()

    def on_state(state: LinkState) -> None:
        if state.status != "connecting" and not first.done():
            first.set_result(state)

    link = BrainLink(
        paths.brain_socket,
        paths.link_token,
        client_version=_client_version(),
        tools=HostToolDispatcher(_NoTools()),
        on_state=on_state,
    )
    link.start()
    try:
        async with asyncio.timeout(seconds):
            state = await first
    except TimeoutError:
        return Probe(False, "timeout")
    finally:
        with contextlib.suppress(Exception):
            await link.stop()
    if state.status == "up":
        return Probe(True, models=state.models)
    return Probe(False, state.reason)


def run_probe(paths: WaddlePaths) -> Probe:
    return asyncio.run(probe_brain(paths))


def _roles_wanted(what: str) -> tuple[Role, ...] | None:
    if what == ALL:
        return ROLES
    return next(((role,) for role in ROLES if role == what), None)


def pull(ctl: Ctl, what: str, configured: Configured = configured_models) -> int:
    roles = _roles_wanted(what)
    if roles is None:
        print(
            f"Waddle downloads models by role only: {', '.join(ROLES)} or {ALL}.\n"
            "Which model plays a role is set in deploy/brain.toml.",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    try:
        names = configured()
    except ComposeError:
        print("Waddle's brain config (deploy/brain.toml) is missing or not valid.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    missing = [role for role in roles if role not in names]
    if what != ALL and missing:
        print(f"{what}: deploy/brain.toml names no model for this role.", file=sys.stderr)
        return EXIT_FAILED
    # A download prints Docker's own output, so BrainCtl cannot tell why one failed. Ask
    # first whether Docker can be used at all: that failure has a plain sentence.
    ready = ctl.status()
    if not ready.ok:
        print(ready.reason or "Docker could not be asked.", file=sys.stderr)
        print("Nothing was downloaded. See what is missing with: waddle doctor", file=sys.stderr)
        return exit_code(ready)
    for index, role in enumerate(roles):
        name = names.get(role)
        if name is None:
            print(f"{role}: deploy/brain.toml names no model for this role.", file=sys.stderr)
            return EXIT_FAILED
        print(f"{role}: downloading {name}")
        print("  The download shows its own sizes and progress below. It can be many gigabytes.")
        sys.stdout.flush()  # our lines first: the download writes straight to the terminal
        result = ctl.pull_model(role, progress=True)
        if not result.ok:
            print(f"{role}: {result.reason or 'the download failed.'}", file=sys.stderr)
            left = ", ".join(roles[index:])
            print(f"Not downloaded: {left}. Try again with: waddle models pull {role}")
            return exit_code(result)
        print(f"{role}: done")
    print("Check with: waddle models status")
    return EXIT_OK


def model_line(role: str, name: str, probe: Probe) -> tuple[str, bool | None]:
    """One status line, and whether the model is present (None: could not be checked)."""
    if not probe.up:
        return f"{role}: {name}: not checked ({probe.why()})", None
    seen = probe.model(role)
    if seen is None:
        return f"{role}: {name}: not checked (the brain did not report this role)", None
    if not seen.available:
        return f"{role}: {name}: not downloaded", False
    return f"{role}: {name}: present{', loaded' if seen.loaded else ''}", True


def status(
    paths: WaddlePaths,
    configured: Configured = configured_models,
    probe: Callable[[WaddlePaths], Probe] = run_probe,
) -> int:
    try:
        names = configured()
    except ComposeError:
        print("Waddle's brain config (deploy/brain.toml) is missing or not valid.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    answer = probe(paths)
    present: list[bool | None] = []
    for role in ROLES:
        if role not in names:
            print(f"{role}: no model configured")
            continue
        line, there = model_line(role, names[role], answer)
        print(line)
        present.append(there)
    if not answer.up:
        hint = {
            "no_socket": "Start the brain with: waddle brain up",
            "busy": "Stop Waddle (waddle stop) and ask again.",
        }.get(answer.reason, "See what is wrong with: waddle doctor")
        print(hint)
        return EXIT_UNAVAILABLE
    if False in present:
        print("Download what is missing with: waddle models pull all")
        return EXIT_FAILED
    return EXIT_OK if None not in present else EXIT_UNAVAILABLE
