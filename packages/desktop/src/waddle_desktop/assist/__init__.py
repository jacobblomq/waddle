"""Host services behind a question: the link, the host tools, app identity, the asker.

No Qt here. Everything built by ``build_assist`` lives on the desktop's I/O loop; the UI
layer forwards the callbacks as queued signals and calls in with ``IoThread.call`` /
``IoThread.submit``:

    assist = build_assist(paths, senses, client_version=..., cache=identity_cache,
                          on_delta=..., on_done=..., on_error=..., on_link_state=...)
    io.call(assist.start)                      # begin reaching for the brain
    io.submit(assist.asker.ask("what is this?"))
    io.call(assist.asker.prewarm)              # when the Ask window opens
    io.call(assist.asker.cancel)
    io.submit(assist.stop())                   # before the senses stop
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from waddle_core.capabilities import Capabilities
from waddle_core.paths import WaddlePaths
from waddle_desktop import log
from waddle_desktop.assist import sight
from waddle_desktop.assist.asker import AskController, Sent
from waddle_desktop.assist.backends import HostBackends
from waddle_desktop.assist.identity_service import Current, IdentityService
from waddle_desktop.assist.shared_exec import SharedExec
from waddle_desktop.identity.cache import IdentityCache
from waddle_desktop.identity.packs import PackIndex, default_packs_dir, load_packs
from waddle_desktop.link.client import BrainLink, Connector, LinkState
from waddle_desktop.link.token import TokenError, ensure_token
from waddle_desktop.link.tools_host import HostToolDispatcher
from waddle_desktop.sandbox.readonly_exec import TOOL_DIRS, find_tool
from waddle_desktop.sensors.senses import Senses

__all__ = [
    "AskController",
    "Assist",
    "Current",
    "HostBackends",
    "IdentityService",
    "Sent",
    "SharedExec",
    "build_assist",
]


def _installed(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


@dataclass(frozen=True)
class Assist:
    """The wired host services. Call ``start`` and everything else on the I/O loop."""

    link: BrainLink
    tools: HostToolDispatcher
    backends: HostBackends
    identity: IdentityService
    asker: AskController
    sandbox: SharedExec
    packs: PackIndex  # ``packs.errors`` lists skipped pack files, for ``waddle doctor``
    senses: Senses
    capture_bin: Path
    token_error: str | None  # a ``TokenError`` code if the link token could not be set up

    def start(self) -> None:
        """Begin connecting to the brain, and identify the window already in front."""
        self.identity.on_eyes(self.senses.state)
        self.link.start()

    async def stop(self) -> None:
        self.asker.cancel()
        await self.identity.stop()
        await self.link.stop()

    async def check_sandbox(self) -> bool:
        """Run the sandbox self-check now (it otherwise runs on first use). True if it works."""
        return await self.sandbox.ready() is not None

    def capabilities(self) -> Capabilities:
        """What works right now. Cheap (a few file checks); starts nothing."""
        link = self.link.state
        # UNVERIFIED (spike S3.2): the link does not say which models read images, so "some
        # answering model is present" stands in for "a model can read a screenshot".
        vision = link.status == "up" and any(
            model.available and model.role != "embedder" for model in link.models
        )
        capture, capture_reason = sight.capture_capability(
            self.senses.state, helper_installed=_installed(self.capture_bin)
        )
        sandbox, sandbox_reason = self.sandbox.capability
        return Capabilities(
            brain=link.status,
            brain_reason=link.reason,
            vision=vision,
            capture=capture,
            capture_reason=capture_reason,
            sandbox=sandbox,
            sandbox_reason=sandbox_reason,
            packs=len(self.packs.packs),
            pack_errors=len(self.packs.errors),
            man=find_tool([f"{directory}/man" for directory in TOOL_DIRS]) is not None,
            tldr=find_tool([f"{directory}/tldr" for directory in TOOL_DIRS]) is not None,
        )


def build_assist(
    paths: WaddlePaths,
    senses: Senses,
    *,
    client_version: str,
    cache: IdentityCache | None,
    on_delta: Callable[[str], None],
    on_done: Callable[[tuple[str, ...], str | None], None],
    on_error: Callable[[str], None],
    on_link_state: Callable[[LinkState], None] | None = None,
    on_identity: Callable[[], None] | None = None,
    on_sent: Callable[[Sent], None] | None = None,
    packs: PackIndex | None = None,
    sandbox: SharedExec | None = None,
    connector: Connector | None = None,
    make_token: Callable[[Path], str] = ensure_token,
) -> Assist:
    """Wire the link, the host tools, identity and the asker around ``senses``.

    Makes sure the link token exists first (the brain's container mounts it), loads the app
    packs once, and registers the identity service as a listener of the senses. Nothing
    connects and no process starts until ``Assist.start`` runs on the I/O loop. All
    callbacks are called on that loop's thread. ``cache`` is the identity cache the senses'
    ``identify`` already uses. The keyword arguments after ``on_sent`` are for tests.
    """
    token_error: str | None = None
    try:
        make_token(paths.link_token)
    except TokenError as exc:
        # The link will report down/"no_token"; Waddle still starts.
        token_error = exc.code
        log.warning("link_token_failed", code=exc.code)

    packs = load_packs(default_packs_dir()) if packs is None else packs
    sandbox = SharedExec() if sandbox is None else sandbox
    identity = IdentityService(
        eyes=lambda: senses.state,
        packs=packs,
        cache=cache,
        sandbox=sandbox,
        on_change=on_identity,
    )
    senses.add_listener(identity.on_eyes)
    backends = HostBackends(
        eyes=lambda: senses.state,
        identity=identity.current,
        sandbox=sandbox,
        helper_installed=lambda: _installed(paths.capture_bin),
    )
    tools = HostToolDispatcher(backends)
    askers: list[AskController] = []

    def link_state(state: LinkState) -> None:
        for asker in askers:
            asker.link_state(state)

    link = BrainLink(
        paths.brain_socket,
        paths.link_token,
        client_version=client_version,
        tools=tools,
        on_state=link_state,
        connector=connector,
    )
    asker = AskController(
        link=link,
        senses=senses,
        identity=identity.current,
        on_delta=on_delta,
        on_done=on_done,
        on_error=on_error,
        on_state=on_link_state,
        on_sent=on_sent,
    )
    askers.append(asker)
    return Assist(
        link=link,
        tools=tools,
        backends=backends,
        identity=identity,
        asker=asker,
        sandbox=sandbox,
        packs=packs,
        senses=senses,
        capture_bin=paths.capture_bin,
        token_error=token_error,
    )
