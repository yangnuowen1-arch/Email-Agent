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
    ScriptedLLMGateway,
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


def test_response_can_be_replayed_as_an_assistant_tool_call_message() -> None:
    response = LLMResponse(
        tool_calls=[ToolCall(id="call_1", name="search_mail", arguments={"query": "quote"})]
    )
    assistant_message = response.as_assistant_message()

    request = LLMRequest(
        messages=[
            LLMMessage(role=LLMMessageRole.USER, content="Find the quote"),
            assistant_message,
            LLMMessage(
                role=LLMMessageRole.TOOL,
                content='{"tool_name":"search_mail","ok":true}',
                tool_call_id="call_1",
                tool_name="search_mail",
            ),
        ],
        tools=[_tool_definition()],
    )

    assert request.messages[1].tool_calls[0].id == "call_1"
    assert request.messages[2].tool_call_id == "call_1"


async def test_scripted_gateway_returns_scripted_tool_call_and_records_request() -> None:
    gateway = ScriptedLLMGateway(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="call_1", name="search_mail", arguments={"query": "quote"})]
            )
        ]
    )
    request = LLMRequest(
        messages=[LLMMessage(role=LLMMessageRole.USER, content="Find the quote")],
        tools=[_tool_definition()],
    )

    response = await gateway.generate(request)

    assert response.tool_calls[0].name == "search_mail"
    assert gateway.requests == [request]
    gateway.assert_exhausted()


async def test_scripted_gateway_fails_when_the_agent_makes_an_extra_model_turn() -> None:
    gateway = ScriptedLLMGateway([LLMResponse(text="Done")])
    request = LLMRequest(messages=[LLMMessage(role=LLMMessageRole.USER, content="Hello")])

    await gateway.generate(request)

    with pytest.raises(AssertionError, match="more requests"):
        await gateway.generate(request)


def test_gateway_contract_rejects_ambiguous_or_invalid_messages() -> None:
    with pytest.raises(ValidationError, match="text or at least one tool call"):
        LLMResponse()

    with pytest.raises(ValidationError, match="require tool_call_id"):
        LLMMessage(role=LLMMessageRole.TOOL, content="{}")

    with pytest.raises(ValidationError, match="require JSON content"):
        LLMMessage(
            role=LLMMessageRole.TOOL,
            content="not json",
            tool_call_id="call_1",
        )

    with pytest.raises(ValidationError, match="require JSON content"):
        LLMMessage(
            role=LLMMessageRole.TOOL,
            content='{"value": NaN}',
            tool_call_id="call_1",
        )

    with pytest.raises(ValidationError, match="preceding tool call"):
        LLMRequest(
            messages=[
                LLMMessage(role=LLMMessageRole.USER, content="Hello"),
                LLMMessage(
                    role=LLMMessageRole.TOOL,
                    content="{}",
                    tool_call_id="call_1",
                ),
            ]
        )

    with pytest.raises(ValidationError, match="corresponding tool result"):
        LLMRequest(
            messages=[
                LLMMessage(role=LLMMessageRole.USER, content="Hello"),
                LLMResponse(
                    tool_calls=[ToolCall(id="call_1", name="search_mail", arguments={})]
                ).as_assistant_message(),
            ]
        )

    with pytest.raises(ValidationError):
        LLMResponse(text="x" * 100_001)
