"""Bounded LangGraph orchestration for provider-neutral tool calling."""

from __future__ import annotations

import asyncio
import operator
import time
from typing import Annotated, Literal, TypedDict

from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from app.agent.errors import (
    AgentNodeError,
    NonRetryableAgentNodeError,
    TransientAgentNodeError,
)
from app.agent.models import (
    AgentNodeErrorKind,
    AgentNodeEvent,
    AgentNodeName,
    AgentRunRequest,
    AgentRunResult,
    AgentTerminationReason,
    AgentToolEvent,
)
from app.llm import (
    LLMGateway,
    LLMMessage,
    LLMMessageRole,
    LLMRequest,
    NonRetryableLLMError,
    ToolCall,
    TransientLLMError,
)
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
    node_events: list[AgentNodeEvent]
    node_failure_attempts: dict[AgentNodeName, int]
    node_retry_max_attempts: int


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

    # 这里回路了
    def _build_graph(self, request: AgentRunRequest):
        retry_policy = self._node_retry_policy(request)
        builder = StateGraph(AgentState, context_schema=AgentRuntimeContext)
        builder.add_node(
            "model",
            self._model,
            retry_policy=retry_policy,
            error_handler=self._handle_node_error,
        )
        builder.add_node(
            "tools",
            self._dispatch_tools,
            retry_policy=retry_policy,
            error_handler=self._handle_node_error,
        )
        builder.add_edge(START, "model")
        builder.add_conditional_edges(
            "model",
            self._route_after_model,
            {"tools": "tools", "end": END},
        )
        builder.add_edge("tools", "model")
        return builder.compile()

    @staticmethod
    def _node_retry_policy(request: AgentRunRequest) -> RetryPolicy:
        """Create a deterministic, bounded retry policy for one graph run."""

        return RetryPolicy(
            initial_interval=request.node_retry_initial_interval_seconds,
            backoff_factor=2.0,
            max_interval=10.0,
            max_attempts=request.node_retry_max_attempts,
            jitter=False,
            retry_on=TransientAgentNodeError,
        )

    # AgentRunner
    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Run one trusted request and return its safe terminal result."""

        node_events: list[AgentNodeEvent] = []
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
        # 核心句 直接Loop.  Retry policy is request-scoped, so each run gets
        # its own compiled graph rather than mutating a graph shared by runs.
        result = await self._build_graph(request).ainvoke(
            initial_state,
            {"recursion_limit": self._recursion_limit(request.max_steps)},
            context={
                "tool_context": ToolContext(
                    allowed_account_ids=frozenset(request.allowed_account_ids)
                ),
                "node_events": node_events,
                "node_failure_attempts": {},
                "node_retry_max_attempts": request.node_retry_max_attempts,
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
            node_events=tuple(node_events),
        )

    # AgentRunner
    async def _model(
        self,
        state: AgentState,
        runtime: Runtime[AgentRuntimeContext],
    ) -> dict[str, object]:
        """Ask the gateway for text or tool calls, retaining a replayable turn."""

        try:
            if state["model_turns"] >= state["max_steps"]:
                update: dict[str, object] = {
                    "pending_tool_calls": [],
                    "termination_reason": AgentTerminationReason.MAX_STEPS,
                }
            else:
                model_turns = state["model_turns"] + 1
                response = await asyncio.wait_for(
                    self._gateway.generate(
                        LLMRequest(
                            messages=state["messages"],
                            tools=list(self._tools.definitions),
                        )
                    ),
                    timeout=state["model_timeout_seconds"],
                )
                assistant_message = response.as_assistant_message()

                if response.tool_calls:
                    if len(response.tool_calls) > state["max_tool_calls_per_turn"]:
                        update = {
                            "messages": [assistant_message],
                            "model_turns": model_turns,
                            "pending_tool_calls": [],
                            "termination_reason": AgentTerminationReason.TOOL_CALL_LIMIT,
                        }
                    elif model_turns >= state["max_steps"]:
                        update = {
                            "messages": [assistant_message],
                            "model_turns": model_turns,
                            "pending_tool_calls": [],
                            "termination_reason": AgentTerminationReason.MAX_STEPS,
                        }
                    else:
                        update = {
                            "messages": [assistant_message],
                            "model_turns": model_turns,
                            "pending_tool_calls": response.tool_calls,
                        }
                else:
                    update = {
                        "messages": [assistant_message],
                        "model_turns": model_turns,
                        "pending_tool_calls": [],
                        "answer": response.text,
                        "termination_reason": AgentTerminationReason.COMPLETED,
                    }
        except Exception as exc:  # noqa: BLE001 - normalize the provider boundary
            node_error = self._classify_node_error(AgentNodeName.MODEL, exc)
            self._record_node_failure(runtime, node_error)
            if node_error is exc:
                raise
            raise node_error from exc

        self._record_node_success(runtime, AgentNodeName.MODEL)
        return update

    async def _dispatch_tools(
        self,
        state: AgentState,
        runtime: Runtime[AgentRuntimeContext],
    ) -> dict[str, object]:
        """Run each requested tool through the registry and append observations."""

        try:
            messages: list[LLMMessage] = []
            events: list[AgentToolEvent] = []
            tool_context = runtime.context["tool_context"]

            for call in state["pending_tool_calls"]:
                started = time.monotonic()
                result = await self._tools.invoke(call.name, call.arguments, tool_context)
                duration_ms = int((time.monotonic() - started) * 1000)
                messages.append(self._tool_observation_message(call, result))
                events.append(self._tool_event(call, result, duration_ms))
            update: dict[str, object] = {
                "messages": messages,
                "tool_events": events,
                "pending_tool_calls": [],
            }
        except Exception as exc:  # noqa: BLE001 - only unexpected dispatch faults escape tools
            node_error = self._classify_node_error(AgentNodeName.TOOLS, exc)
            self._record_node_failure(runtime, node_error)
            if node_error is exc:
                raise
            raise node_error from exc

        self._record_node_success(runtime, AgentNodeName.TOOLS)
        return update

    def _handle_node_error(
        self,
        state: AgentState,
        error: NodeError,
        runtime: Runtime[AgentRuntimeContext],
    ) -> dict[str, object]:
        """Convert a terminal node failure into a safe graph result.

        LangGraph invokes this only after the node's matching retry policy has
        been exhausted, or immediately for a non-retryable error.
        """

        node_error = error.error
        if not isinstance(node_error, AgentNodeError):
            node_error = NonRetryableAgentNodeError(
                node=AgentNodeName(error.node),
                error_kind=AgentNodeErrorKind.NON_RETRYABLE,
                termination_reason=AgentTerminationReason.NON_RETRYABLE_ERROR,
            )
            self._record_node_failure(runtime, node_error)

        update: dict[str, object] = {
            "pending_tool_calls": [],
            "termination_reason": node_error.termination_reason,
        }
        if node_error.node is AgentNodeName.MODEL:
            # A failed/retried model invocation is one logical model turn, just
            # as it was before node retries existed.  Retries themselves are
            # represented by ``node_events`` rather than inflating this count.
            update["model_turns"] = state["model_turns"] + 1
        return update

    @staticmethod
    def _classify_node_error(
        node: AgentNodeName,
        exc: Exception,
    ) -> AgentNodeError:
        if isinstance(exc, AgentNodeError):
            return exc
        if isinstance(exc, TimeoutError):
            return TransientAgentNodeError(
                node=node,
                error_kind=AgentNodeErrorKind.TIMEOUT,
                termination_reason=(
                    AgentTerminationReason.MODEL_TIMEOUT
                    if node is AgentNodeName.MODEL
                    else AgentTerminationReason.RETRY_EXHAUSTED
                ),
            )
        if isinstance(exc, TransientLLMError):
            return TransientAgentNodeError(
                node=node,
                error_kind=AgentNodeErrorKind.TRANSIENT,
                termination_reason=AgentTerminationReason.RETRY_EXHAUSTED,
            )
        if isinstance(exc, NonRetryableLLMError):
            return NonRetryableAgentNodeError(
                node=node,
                error_kind=AgentNodeErrorKind.NON_RETRYABLE,
                termination_reason=AgentTerminationReason.NON_RETRYABLE_ERROR,
            )
        return NonRetryableAgentNodeError(
            node=node,
            error_kind=AgentNodeErrorKind.NON_RETRYABLE,
            termination_reason=AgentTerminationReason.NON_RETRYABLE_ERROR,
        )

    @staticmethod
    def _record_node_failure(
        runtime: Runtime[AgentRuntimeContext],
        error: AgentNodeError,
    ) -> None:
        attempts = runtime.context["node_failure_attempts"]
        attempt = attempts.get(error.node, 0) + 1
        attempts[error.node] = attempt
        runtime.context["node_events"].append(
            AgentNodeEvent(
                node=error.node,
                attempt=attempt,
                error_kind=error.error_kind,
                retryable=error.retryable,
                will_retry=(
                    error.retryable and attempt < runtime.context["node_retry_max_attempts"]
                ),
            )
        )

    @staticmethod
    def _record_node_success(
        runtime: Runtime[AgentRuntimeContext],
        node: AgentNodeName,
    ) -> None:
        """Reset retry-attempt numbering after an invocation eventually succeeds."""

        runtime.context["node_failure_attempts"].pop(node, None)

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
