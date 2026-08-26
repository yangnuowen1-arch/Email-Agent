"""Provider-neutral LLM gateway contracts.

Mirrors the gateway abstraction used by the GoldAgent project: the interface is
decoupled from any concrete provider so alternative SDKs can be dropped in later.
The concrete :class:`~app.llm.gateway.OpenAIGateway` implementation lives in
:mod:`app.llm.gateway`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMError(Exception):
    """Base error for the LLM gateway layer."""


class LLMConfigurationError(LLMError):
    """Raised when the gateway is misconfigured (e.g. missing API key)."""


class LLMProviderError(LLMError):
    """Raised when an upstream LLM provider request fails."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider


@dataclass(slots=True)
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ChatResponse:
    """Normalized completion result returned by every gateway implementation."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


class LLMGateway(Protocol):
    """Contract every concrete gateway (OpenAI, future providers) must satisfy."""

    async def chat(
        self, messages: list[BaseMessage], tools: list[BaseTool] | None = None
    ) -> ChatResponse: ...

    async def stream(
        self, messages: list[BaseMessage], tools: list[BaseTool] | None = None
    ) -> AsyncIterator[str]: ...

    def bind_tools(self, tools: list[BaseTool]) -> LLMGateway: ...

    async def structured_output(
        self, messages: list[BaseMessage], schema: type[SchemaT]
    ) -> SchemaT: ...
