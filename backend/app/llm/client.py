"""Provider-neutral LLM gateway contracts, including replayable tool calls."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.tools import ToolDefinition


def _reject_non_json_constant(value: str) -> None:
    """Reject JavaScript-style constants accepted by Python's permissive decoder."""

    raise ValueError(f"invalid JSON constant: {value}")


class LLMMessageRole(StrEnum):
    """The provider-neutral roles accepted by the gateway."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """A validated request from a model to invoke one registered tool."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: dict[str, Any]

    @field_validator("arguments")
    @classmethod
    def _requires_json_compatible_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Keep calls safe to persist or replay as a provider-neutral transcript."""

        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("tool arguments must be a JSON-compatible object") from exc
        return value


class LLMMessage(BaseModel):
    """One serializable message passed to a model provider.

    Assistant messages retain the complete tool-call envelope, which lets the
    next provider request pair a tool result with the model-issued call ID.
    """

    model_config = ConfigDict(extra="forbid")

    role: LLMMessageRole
    content: str | None = Field(default=None, max_length=100_000)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)
    tool_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")

    @model_validator(mode="after")
    def _requires_role_specific_fields(self) -> Self:
        if self.role is LLMMessageRole.ASSISTANT:
            if self.tool_call_id is not None or self.tool_name is not None:
                raise ValueError("assistant messages cannot include tool result fields")
            if self.content is None and not self.tool_calls:
                raise ValueError("assistant messages require text or at least one tool call")
            call_ids = [call.id for call in self.tool_calls]
            if len(call_ids) != len(set(call_ids)):
                raise ValueError("assistant message tool call IDs must be unique")
            return self

        if self.tool_calls:
            raise ValueError("only assistant messages may include tool calls")

        if self.role is LLMMessageRole.TOOL:
            if self.tool_call_id is None:
                raise ValueError("tool messages require tool_call_id")
            if self.content is None:
                raise ValueError("tool messages require JSON content")
            try:
                payload = json.loads(self.content, parse_constant=_reject_non_json_constant)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError("tool messages require JSON content") from exc
            if not isinstance(payload, dict):
                raise ValueError("tool messages require a JSON object")
            return self

        if self.tool_call_id is not None or self.tool_name is not None:
            raise ValueError("only tool messages may include tool result fields")
        if self.content is None:
            raise ValueError(f"{self.role.value} messages require text content")
        return self


class LLMRequest(BaseModel):
    """A gateway request containing conversation context and available tools."""

    model_config = ConfigDict(extra="forbid")

    messages: list[LLMMessage] = Field(min_length=1)
    tools: list[ToolDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _requires_a_valid_replayable_transcript(self) -> Self:
        tool_names = [tool.name for tool in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("tool definitions must have unique names")

        pending_call_names: dict[str, str] = {}
        seen_call_ids: set[str] = set()
        for message in self.messages:
            if message.role is LLMMessageRole.TOOL:
                call_id = message.tool_call_id
                if call_id is None:
                    raise ValueError("tool messages require tool_call_id")
                expected_name = pending_call_names.pop(call_id, None)
                if expected_name is None:
                    raise ValueError("tool message must reference a preceding tool call")
                if message.tool_name is not None and message.tool_name != expected_name:
                    raise ValueError("tool message name must match the referenced tool call")
                continue

            if pending_call_names:
                raise ValueError(
                    "all preceding tool calls require a tool result before the next turn"
                )

            if message.role is LLMMessageRole.ASSISTANT:
                for call in message.tool_calls:
                    if call.id in seen_call_ids:
                        raise ValueError("tool call IDs must be unique across a transcript")
                    seen_call_ids.add(call.id)
                    pending_call_names[call.id] = call.name
        if pending_call_names:
            raise ValueError("all assistant tool calls require a corresponding tool result")
        return self


class LLMResponse(BaseModel):
    """A model turn that contains text, tool calls, or both."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=100_000)
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_a_model_action(self) -> Self:
        if self.text is None and not self.tool_calls:
            raise ValueError("an LLM response needs text or at least one tool call")
        call_ids = [call.id for call in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("LLM response tool call IDs must be unique")
        return self

    def as_assistant_message(self) -> LLMMessage:
        """Convert this turn into the replayable history entry for the next turn."""

        return LLMMessage(
            role=LLMMessageRole.ASSISTANT,
            content=self.text,
            tool_calls=self.tool_calls,
        )


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


class ScriptedLLMGateway:
    """Deterministic, request-recording gateway for agent and gateway tests.

    This test double intentionally has no provider behavior: callers explicitly
    script each response, including tool calls, and can assert that the graph
    made exactly the expected number of model turns.
    """

    def __init__(self, responses: Iterable[LLMResponse]) -> None:
        self._responses = deque(response.model_copy(deep=True) for response in responses)
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Record a deep copy of one request and return its next scripted response."""

        self.requests.append(request.model_copy(deep=True))
        if not self._responses:
            raise AssertionError("ScriptedLLMGateway received more requests than responses")
        return self._responses.popleft().model_copy(deep=True)

    def assert_exhausted(self) -> None:
        """Fail a test when a scripted response was never consumed."""

        if self._responses:
            count = len(self._responses)
            raise AssertionError(f"ScriptedLLMGateway has {count} unused response(s)")
