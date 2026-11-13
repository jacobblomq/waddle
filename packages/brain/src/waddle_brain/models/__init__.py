"""The model layer (ARCHITECTURE §5.3): providers that satisfy ``contracts.ModelProvider``.

Which model plays which role comes from ``BrainConfig`` only; no model name appears here.
"""

from __future__ import annotations

from waddle_brain.config import BrainConfig
from waddle_brain.contracts import ModelProvider
from waddle_brain.models.ollama import OllamaProvider


def build_provider(config: BrainConfig) -> ModelProvider:
    """The provider the config asks for. Only Ollama exists so far."""
    if config.provider == "ollama":
        return OllamaProvider(config)
    raise ValueError("unknown provider")  # unreachable while the config allows only "ollama"
