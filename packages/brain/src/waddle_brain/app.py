"""The brain's entry point: load config and token, serve the link until told to stop.

Exit codes: 0 clean stop, 2 config or token problem, 3 the socket could not be set up.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path
from typing import Final

from waddle_brain import log
from waddle_brain.config import BrainConfig, BrainConfigError, load_brain_config
from waddle_brain.contracts import Answerer, ModelProvider
from waddle_brain.server import LinkServer, LinkServerError

DEFAULT_CONFIG: Final = "/etc/waddle/brain.toml"
CONFIG_ENV: Final = "WADDLE_BRAIN_CONFIG"
_TOKEN: Final = re.compile(r"[0-9a-f]{64}")  # the same shape the hello message requires


class TokenError(ValueError):
    """The link token file is missing or malformed. ``code`` is safe to log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def read_token(path: Path) -> str:
    """The link token from its file. The token itself never appears in an error or a log."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        raise TokenError("token_unreadable") from None
    if not _TOKEN.fullmatch(text):
        raise TokenError("token_malformed")
    return text


def _version() -> str:
    try:
        return metadata.version("waddle-brain")
    except metadata.PackageNotFoundError:
        return "0"


async def _serve(
    config: BrainConfig, token: str, provider: ModelProvider, answerer: Answerer
) -> int:
    """Run the link server until SIGTERM or SIGINT, then shut everything down."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    server = LinkServer(config, answerer, token=token, version=_version())
    try:
        await server.start()
        await stop.wait()
    except LinkServerError as exc:
        log.warning("brain_start_failed", code=exc.code)
        return 3
    finally:
        await server.stop()
        await provider.close()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waddle-brain", description="Waddle's brain.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG),
        help=f"config file (default: ${CONFIG_ENV} or {DEFAULT_CONFIG})",
    )
    args = parser.parse_args(argv)
    log.configure()
    try:
        config = load_brain_config(args.config)
        token = read_token(config.token_path)
    except BrainConfigError:
        log.warning("brain_start_failed", code="config_invalid")
        return 2
    except TokenError as exc:
        log.warning("brain_start_failed", code=exc.code)
        return 2
    # Imported here so the model stack is only loaded once the config and token are good.
    from waddle_brain.models import build_provider
    from waddle_brain.qa import build_answerer

    provider = build_provider(config)
    answerer = build_answerer(config, provider)
    log.event("brain_starting")
    code = asyncio.run(_serve(config, token, provider, answerer))
    log.event("brain_stopped", code=code)
    return code
