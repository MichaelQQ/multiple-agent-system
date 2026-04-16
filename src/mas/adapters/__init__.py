from __future__ import annotations

from .base import Adapter, DispatchHandle
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .gemini_cli import GeminiCliAdapter
from .ollama import OllamaAdapter
from .opencode import OpenCodeAdapter
from .mock import MockAdapter

REGISTRY: dict[str, type[Adapter]] = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiCliAdapter,
    "ollama": OllamaAdapter,
    "opencode": OpenCodeAdapter,
    "mock": MockAdapter,
}


def get_adapter(name: str) -> type[Adapter]:
    if name not in REGISTRY:
        raise KeyError(f"unknown provider: {name}")
    return REGISTRY[name]


__all__ = ["Adapter", "DispatchHandle", "get_adapter", "REGISTRY"]
