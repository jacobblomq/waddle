"""Question answering with screen context (ARCHITECTURE §5.3, §5.6)."""

from __future__ import annotations

from waddle_brain.config import BrainConfig
from waddle_brain.contracts import Answerer, ModelProvider
from waddle_brain.qa.answerer import QaAnswerer

__all__ = ["QaAnswerer", "build_answerer"]


def build_answerer(config: BrainConfig, provider: ModelProvider) -> Answerer:
    """The answerer the link server uses for every ``ask``."""
    return QaAnswerer(config, provider)
