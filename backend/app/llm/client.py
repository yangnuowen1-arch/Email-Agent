"""Provider-neutral LLM gateway contracts, including tool calls."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.tools import ToolDefinition


class LLMMessageRole(StrEnum):
    """The provider-neutral roles accepted by the gateway."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class LLMMessage(BaseModel):
    """One serializable message passed to a model provider."""

    model_config = ConfigDict(extra="forbid")

    role: LLMMessageRole
    content: str
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _requires_call_id_for_tool_messages(self) -> Self:
        if self.role is LLMMessageRole.TOOL and self.tool_call_id is None:
            raise ValueError("tool messages require tool_call_id")
        if self.role is not LLMMessageRole.TOOL and self.tool_call_id is not None:
            raise ValueError("only tool messages may include tool_call_id")
        return self


class ToolCall(BaseModel):
    """A validated request from a model to invoke one registered tool."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: dict[str, Any]


class LLMRequest(BaseModel):
    """A gateway request containing conversation context and available tools."""

    model_config = ConfigDict(extra="forbid")

    messages: list[LLMMessage] = Field(min_length=1)
    tools: list[ToolDefinition] = Field(default_factory=list)


class LLMResponse(BaseModel):
    """A model turn that contains text, tool calls, or both."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_a_model_action(self) -> Self:
        if self.text is None and not self.tool_calls:
            raise ValueError("an LLM response needs text or at least one tool call")
        return self


class LLMClient(Protocol):
    """Legacy prompt-completion contract retained for simple callers."""

    async def complete(self, prompt: str) -> LLMResponse:
        """Return a completion for the supplied prompt."""


class LLMGateway(Protocol):
    """Contract for providers that can receive tool definitions and tool calls."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Return text and/or validated tool-call requests for one model turn."""


class EchoLLMClient:
    """Network-free gateway that remains runnable before a provider is configured."""

    async def complete(self, prompt: str) -> LLMResponse:
        """Preserve the original simple completion entry point for local callers."""

        return await self.generate(
            LLMRequest(messages=[LLMMessage(role=LLMMessageRole.USER, content=prompt)])
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Echo the latest user content without fabricating a tool call.

        A real gateway maps ``request.tools`` to the selected provider format and
        converts provider tool calls back into :class:`ToolCall` instances.
        """

        user_content = next(
            (
                message.content
                for message in reversed(request.messages)
                if message.role is LLMMessageRole.USER
            ),
            "",
        )
        return LLMResponse(text=f"[starter response] {user_content}")
