"""Bounded LangGraph orchestration for provider-neutral tool calling."""

from __future__ import annotations

import asyncio
import operator
import time
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.agent.models import (
    AgentRunRequest,
    AgentRunResult,
    AgentTerminationReason,
    AgentToolEvent,
)
from app.llm import LLMGateway, LLMMessage, LLMMessageRole, LLMRequest, ToolCall
from app.schemas.tools import ToolInvocationResult
from app.tools import ToolContext, ToolRegistry


# Agent 运行过程中的 State / 状态对象 , Agent 在运行中不断变化的数据
class AgentState(TypedDict):
    """Serializable state accumulated during one non-persisted graph run."""

    run_id: str
    messages: Annotated[list[LLMMessage], operator.add]
    tool_events: Annotated[list[AgentToolEvent], operator.add]
    model_turns: int
    max_steps: int
    max_tool_calls_per_turn: int
    model_timeout_seconds: float
    pending_tool_calls: list[ToolCall]
    answer: str | None
    termination_reason: AgentTerminationReason | None

#  这次运行携带进来的“可信能力/依赖”
class AgentRuntimeContext(TypedDict):
    """Trusted, non-state dependencies scoped to a single graph invocation."""

  # 服务器允许这个 Agent 访问哪些邮箱账号
    tool_context: ToolContext 



# 总控制器
class EmailAgent:
    """Execute ``model -> tools -> model/end`` with bounded, read-only tools.

    The graph has no implicit persistence and receives all external capabilities
    through constructor injection.  In particular, the model never receives or
    controls the trusted tool authorization scope.
    """

    def __init__(self, gateway: LLMGateway, tools: ToolRegistry) -> None:
        self._gateway = gateway
        self._tools = tools
        self._graph = self._build_graph()

# 这里回路了
    def _build_graph(self):
        builder = StateGraph(AgentState, context_schema=AgentRuntimeContext)
        builder.add_node("model", self._model)
        builder.add_node("tools", self._dispatch_tools)
        builder.add_edge(START, "model")
        builder.add_conditional_edges(
            "model",
            self._route_after_model,
            {"tools": "tools", "end": END},
        )
        builder.add_edge("tools", "model")
        return builder.compile()


# AgentRunner
    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Run one trusted request and return its safe terminal result."""

        initial_state: AgentState = {
            "run_id": request.run_id,
            "messages": list(request.messages),
            "tool_events": [],
            "model_turns": 0,
            "max_steps": request.max_steps,
            "max_tool_calls_per_turn": request.max_tool_calls_per_turn,
            "model_timeout_seconds": request.model_timeout_seconds,
            "pending_tool_calls": [],
            "answer": None,
            "termination_reason": None,
        }
        # 核心句 直接Loop
        result = await self._graph.ainvoke(
            initial_state,
            {"recursion_limit": self._recursion_limit(request.max_steps)},
            context={
                "tool_context": ToolContext(
                    allowed_account_ids=frozenset(request.allowed_account_ids)
                )
            },
        )
        try:
            termination_reason = AgentTerminationReason(result["termination_reason"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("agent graph finished without a terminal reason") from exc

        return AgentRunResult(
            run_id=result["run_id"],
            answer=result.get("answer"),
            model_turns=result["model_turns"],
            termination_reason=termination_reason,
            tool_events=tuple(result.get("tool_events", [])),
        )

# AgentRunner


    async def _model(self, state: AgentState) -> dict[str, object]:
        """Ask the gateway for text or tool calls, retaining a replayable turn."""

        if state["model_turns"] >= state["max_steps"]:
            return {
                "pending_tool_calls": [],
                "termination_reason": AgentTerminationReason.MAX_STEPS,
            }

        model_turns = state["model_turns"] + 1
        try:
            response = await asyncio.wait_for(
                self._gateway.generate(
                    LLMRequest(
                        messages=state["messages"],
                        tools=list(self._tools.definitions),
                    )
                ),
                timeout=state["model_timeout_seconds"],
            )
        except TimeoutError:
            return {
                "model_turns": model_turns,
                "pending_tool_calls": [],
                "termination_reason": AgentTerminationReason.MODEL_TIMEOUT,
            }
        assistant_message = response.as_assistant_message()

        if response.tool_calls:
            if len(response.tool_calls) > state["max_tool_calls_per_turn"]:
                return {
                    "messages": [assistant_message],
                    "model_turns": model_turns,
                    "pending_tool_calls": [],
                    "termination_reason": AgentTerminationReason.TOOL_CALL_LIMIT,
                }
            if model_turns >= state["max_steps"]:
                return {
                    "messages": [assistant_message],
                    "model_turns": model_turns,
                    "pending_tool_calls": [],
                    "termination_reason": AgentTerminationReason.MAX_STEPS,
                }
            return {
                "messages": [assistant_message],
                "model_turns": model_turns,
                "pending_tool_calls": response.tool_calls,
            }

        return {
            "messages": [assistant_message],
            "model_turns": model_turns,
            "pending_tool_calls": [],
            "answer": response.text,
            "termination_reason": AgentTerminationReason.COMPLETED,
        }

    async def _dispatch_tools(
        self,
        state: AgentState,
        runtime: Runtime[AgentRuntimeContext],
    ) -> dict[str, object]:
        """Run each requested tool through the registry and append observations."""

        messages: list[LLMMessage] = []
        events: list[AgentToolEvent] = []
        tool_context = runtime.context["tool_context"]

        for call in state["pending_tool_calls"]:
            started = time.monotonic()
            result = await self._tools.invoke(call.name, call.arguments, tool_context)
            duration_ms = int((time.monotonic() - started) * 1000)
            messages.append(self._tool_observation_message(call, result))
            events.append(self._tool_event(call, result, duration_ms))

        return {
            "messages": messages,
            "tool_events": events,
            "pending_tool_calls": [],
        }

    @staticmethod
    def _route_after_model(state: AgentState) -> Literal["tools", "end"]:
        if state["termination_reason"] is not None:
            return "end"
        return "tools" if state["pending_tool_calls"] else "end"

    @staticmethod
    def _tool_observation_message(
        call: ToolCall,
        result: ToolInvocationResult,
    ) -> LLMMessage:
        return LLMMessage(
            role=LLMMessageRole.TOOL,
            content=result.model_dump_json(),
            tool_call_id=call.id,
            tool_name=call.name,
        )

    @staticmethod
    def _tool_event(
        call: ToolCall,
        result: ToolInvocationResult,
        duration_ms: int,
    ) -> AgentToolEvent:
        return AgentToolEvent(
            tool_call_id=call.id,
            tool_name=call.name,
            ok=result.ok,
            duration_ms=duration_ms,
            error_code=result.error.code if result.error is not None else None,
        )

    @staticmethod
    def _recursion_limit(max_steps: int) -> int:
        """Allow every bounded model turn and its preceding tool dispatch."""

        return max_steps * 2 + 3
