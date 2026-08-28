"""Tests for the minimal LangGraph model-to-tool loop."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from app.agent import AgentRunRequest, AgentTerminationReason, EmailAgent
from app.llm import (
    LLMMessage,
    LLMMessageRole,
    LLMResponse,
    ScriptedLLMGateway,
    ToolCall,
)
from app.schemas.tools import ToolDefinition, ToolInvocationResult
from app.tools import ToolContext, ToolRegistry


class RecordingSearchTool:
    """Small registered tool that makes graph dispatch assertions observable."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], frozenset[int]]] = []

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_mail",
            description="Search mail for test purposes.",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            result_schema={"type": "object"},
        )

    async def invoke(
        self,
        raw_arguments: dict[str, object],
        context: ToolContext,
    ) -> ToolInvocationResult:
        self.calls.append((raw_arguments, context.allowed_account_ids))
        return ToolInvocationResult(
            tool_name="search_mail",
            ok=True,
            result={"items": [{"email_id": 5, "subject": "Quote"}]},
        )


def _request(
    *,
    max_steps: int = 4,
    max_tool_calls_per_turn: int = 8,
    model_timeout_seconds: float = 30.0,
) -> AgentRunRequest:
    return AgentRunRequest(
        run_id="run-test",
        messages=[LLMMessage(role=LLMMessageRole.USER, content="Find the quote")],
        allowed_account_ids=(1,),
        max_steps=max_steps,
        max_tool_calls_per_turn=max_tool_calls_per_turn,
        model_timeout_seconds=model_timeout_seconds,
    )


def test_new_agent_runs_reject_untrusted_assistant_or_tool_history() -> None:
    with pytest.raises(ValidationError, match="only system and user messages"):
        AgentRunRequest(
            messages=[LLMMessage(role=LLMMessageRole.ASSISTANT, content="Prior answer")]
        )


async def test_agent_ends_after_one_text_only_model_turn() -> None:
    gateway = ScriptedLLMGateway([LLMResponse(text="The quote is in the archive.")])
    agent = EmailAgent(gateway, ToolRegistry(()))

    result = await agent.run(_request())

    assert result.answer == "The quote is in the archive."
    assert result.model_turns == 1
    assert result.termination_reason is AgentTerminationReason.COMPLETED
    assert result.tool_events == ()
    assert len(gateway.requests) == 1
    gateway.assert_exhausted()


async def test_agent_replays_assistant_tool_call_and_observation_before_second_turn() -> None:
    tool = RecordingSearchTool()
    gateway = ScriptedLLMGateway(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_search",
                        name="search_mail",
                        arguments={"query": "quote"},
                    )
                ]
            ),
            LLMResponse(text="I found the quote in email 5."),
        ]
    )
    agent = EmailAgent(gateway, ToolRegistry((tool,)))

    result = await agent.run(_request())

    assert result.answer == "I found the quote in email 5."
    assert result.model_turns == 2
    assert result.termination_reason is AgentTerminationReason.COMPLETED
    assert tool.calls == [({"query": "quote"}, frozenset({1}))]
    assert len(result.tool_events) == 1
    assert result.tool_events[0].tool_call_id == "call_search"
    assert result.tool_events[0].tool_name == "search_mail"
    assert result.tool_events[0].ok is True

    second_request_messages = gateway.requests[1].messages
    assistant_message = second_request_messages[-2]
    tool_message = second_request_messages[-1]
    assert assistant_message.role is LLMMessageRole.ASSISTANT
    assert assistant_message.tool_calls[0].id == "call_search"
    assert tool_message.role is LLMMessageRole.TOOL
    assert tool_message.tool_call_id == "call_search"
    assert tool_message.tool_name == "search_mail"
    assert json.loads(tool_message.content or "{}") == {
        "tool_name": "search_mail",
        "ok": True,
        "result": {"items": [{"email_id": 5, "subject": "Quote"}]},
        "error": None,
    }
    gateway.assert_exhausted()


async def test_agent_returns_unknown_tool_as_a_structured_observation() -> None:
    gateway = ScriptedLLMGateway(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="call_missing", name="missing_tool", arguments={})]
            ),
            LLMResponse(text="That tool is not available."),
        ]
    )
    agent = EmailAgent(gateway, ToolRegistry(()))

    result = await agent.run(_request())

    assert result.answer == "That tool is not available."
    assert len(result.tool_events) == 1
    assert result.tool_events[0].ok is False
    assert result.tool_events[0].error_code == "unknown_tool"
    observation = json.loads(gateway.requests[1].messages[-1].content or "{}")
    assert observation["error"]["code"] == "unknown_tool"
    gateway.assert_exhausted()


async def test_agent_stops_before_dispatching_calls_at_the_maximum_turn_budget() -> None:
    tool = RecordingSearchTool()
    gateway = ScriptedLLMGateway(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_search",
                        name="search_mail",
                        arguments={"query": "quote"},
                    )
                ]
            )
        ]
    )
    agent = EmailAgent(gateway, ToolRegistry((tool,)))

    result = await agent.run(_request(max_steps=1))

    assert result.answer is None
    assert result.model_turns == 1
    assert result.termination_reason is AgentTerminationReason.MAX_STEPS
    assert result.tool_events == ()
    assert tool.calls == []
    gateway.assert_exhausted()


async def test_agent_stops_before_dispatching_an_excessive_tool_call_batch() -> None:
    tool = RecordingSearchTool()
    gateway = ScriptedLLMGateway(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call_one", name="search_mail", arguments={"query": "one"}),
                    ToolCall(id="call_two", name="search_mail", arguments={"query": "two"}),
                ]
            )
        ]
    )
    agent = EmailAgent(gateway, ToolRegistry((tool,)))

    result = await agent.run(_request(max_tool_calls_per_turn=1))

    assert result.answer is None
    assert result.model_turns == 1
    assert result.termination_reason is AgentTerminationReason.TOOL_CALL_LIMIT
    assert result.tool_events == ()
    assert tool.calls == []
    gateway.assert_exhausted()


class SlowGateway:
    async def generate(self, request) -> LLMResponse:
        await asyncio.sleep(0.01)
        return LLMResponse(text="Too late")


async def test_agent_returns_a_stable_terminal_state_when_the_model_times_out() -> None:
    agent = EmailAgent(SlowGateway(), ToolRegistry(()))

    result = await agent.run(_request(model_timeout_seconds=0.001))

    assert result.answer is None
    assert result.model_turns == 1
    assert result.termination_reason is AgentTerminationReason.MODEL_TIMEOUT
    assert result.tool_events == ()
