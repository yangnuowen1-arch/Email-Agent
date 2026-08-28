"""Provider-neutral LLM gateway contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.llm import (
    EchoLLMClient,
    LLMMessage,
    LLMMessageRole,
    LLMRequest,
    LLMResponse,
    ToolCall,
)
from app.schemas.tools import ToolDefinition


def _tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name="search_mail",
        description="Search archived mail.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        result_schema={"type": "object"},
    )


async def test_echo_gateway_accepts_messages_and_tool_definitions() -> None:
    gateway = EchoLLMClient()

    response = await gateway.generate(
        LLMRequest(
            messages=[LLMMessage(role=LLMMessageRole.USER, content="Find the quote")],
            tools=[_tool_definition()],
        )
    )

    assert response.text == "[starter response] Find the quote"
    assert response.tool_calls == []


async def test_legacy_complete_uses_the_gateway_contract() -> None:
    response = await EchoLLMClient().complete("Hello")

    assert response.text == "[starter response] Hello"


def test_response_can_express_a_validated_tool_call() -> None:
    response = LLMResponse(
        tool_calls=[ToolCall(id="call_1", name="search_mail", arguments={"query": "quote"})]
    )

    assert response.text is None
    assert response.tool_calls[0].arguments == {"query": "quote"}


def test_gateway_contract_rejects_ambiguous_or_invalid_messages() -> None:
    with pytest.raises(ValidationError, match="text or at least one tool call"):
        LLMResponse()

    with pytest.raises(ValidationError, match="require tool_call_id"):
        LLMMessage(role=LLMMessageRole.TOOL, content="{}")
