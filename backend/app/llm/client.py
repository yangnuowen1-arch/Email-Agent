"""Provider-neutral LLM client protocol and a local placeholder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """The minimal response shape consumed by the agent."""

    text: str


class LLMClient(Protocol):
    """Contract to implement when connecting a real LLM provider."""

    async def complete(self, prompt: str) -> LLMResponse:
        """Return a completion for the supplied prompt."""


class EchoLLMClient:
    """Network-free client that makes the scaffold runnable before provider setup."""

    async def complete(self, prompt: str) -> LLMResponse:
        return LLMResponse(text=f"[starter response] {prompt}")
