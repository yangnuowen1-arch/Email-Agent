"""单邮件意向分析图单元测试：analyze 节点 + 编译后图端到端 + 调用链追踪。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import structlog
from langchain_core.messages import BaseMessage, HumanMessage
from structlog.testing import capture_logs

from app.agent import trace_handle
from app.agent.analysis_graph import (
    EmailAnalysisState,
    _analyze_node,
    build_email_analysis_graph,
)
from app.agent.errors import LLMInvocationError
from app.agent.trace_handle import GraphTraceHandler
from app.schemas.analysis import EmailAnalysisOutput, IntentDetail

# ---------------------------------------------------------------------------
# FakeChatModel（鸭子类型，模拟 with_structured_output）
# ---------------------------------------------------------------------------


class _FakeStructuredRunnable:
    """按调用次数弹出 outcomes：Exception 则抛出，其余经 EmailAnalysisOutput 校验返回。

    outcomes 只剩一个时重复它（RetryPolicy 重试且无后续结果时保持同样表现）。
    """

    def __init__(self, outcomes: list[Any], model: FakeChatModel) -> None:
        self._outcomes = outcomes
        self._model = model

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> Any:
        self._model.received_config = kwargs.get("config")
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, EmailAnalysisOutput):
            return outcome
        return EmailAnalysisOutput.model_validate(outcome)


class FakeChatModel:
    def __init__(
        self,
        *,
        output: Any = None,
        error: Exception | None = None,
        flaky: Exception | None = None,
    ):
        """``flaky``：首次调用抛该异常，之后按 output/error 表现（验证 RetryPolicy）。"""
        outcomes: list[Any] = []
        if flaky is not None:
            outcomes.append(flaky)
        outcomes.append(error if error is not None else output)
        self._outcomes = outcomes
        self.model_name = "fake-model"
        self.received_config: Any = None

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeStructuredRunnable:
        return _FakeStructuredRunnable(self._outcomes, self)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _base_state(**overrides: Any) -> EmailAnalysisState:
    defaults: EmailAnalysisState = {
        "email_id": 1,
        "account_id": 1,
        "subject": "测试邮件",
        "sender": "user@example.com",
        "sent_at": datetime(2026, 8, 28, tzinfo=UTC),
        "cleaned_text": "请帮我取消订单 ORD-123",
    }
    defaults.update(overrides)  # type: ignore[call-overload]
    return defaults


def _success_output() -> EmailAnalysisOutput:
    return EmailAnalysisOutput(
        primary_intent="cancel_order",
        intents=[
            IntentDetail(
                category="cancel_order",
                confidence=0.95,
                reasoning="用户明确要求取消订单 ORD-123",
            )
        ],
        reasoning_summary="用户要求取消订单",
        entities={"order_id": "ORD-123"},
        sentiment="negative",
        priority="P1",
        suggested_tools=[],
    )


# ---------------------------------------------------------------------------
# analyze_node 测试
# ---------------------------------------------------------------------------


async def test_analyze_node_normal() -> None:
    model = FakeChatModel(output=_success_output())
    result = await _analyze_node(_base_state(), chat_model=model)

    assert result["primary_intent"] == "cancel_order"
    assert result["intents"][0]["category"] == "cancel_order"
    assert result["priority"] == "P1"
    assert result["sentiment"] == "negative"
    assert result["llm_model"] == "fake-model"
    assert "error" not in result


async def test_analyze_node_llm_error() -> None:
    model = FakeChatModel(error=RuntimeError("LLM timeout"))
    with pytest.raises(LLMInvocationError) as exc_info:
        await _analyze_node(_base_state(), chat_model=model)

    assert isinstance(exc_info.value.__cause__, RuntimeError)


async def test_analyze_node_invalid_output() -> None:
    """模型返回非法值（intents=[]）导致 Pydantic 校验失败 → 包装为解析失败异常。"""
    model = FakeChatModel(output={"primary_intent": "x", "intents": [], "reasoning_summary": "t"})
    with pytest.raises(LLMInvocationError, match="LLM 输出解析失败"):
        await _analyze_node(_base_state(), chat_model=model)


async def test_analyze_node_out_of_whitelist_intent_fails() -> None:
    """LLM 返回白名单外意图 → Pydantic 校验失败 → 解析失败异常。"""
    model = FakeChatModel(
        output={
            "primary_intent": "resume_request",
            "intents": [{"category": "resume_request", "confidence": 0.9, "reasoning": "新场景"}],
            "reasoning_summary": "t",
        }
    )
    with pytest.raises(LLMInvocationError, match="LLM 输出解析失败"):
        await _analyze_node(_base_state(), chat_model=model)


async def test_analyze_node_empty_body() -> None:
    """空 cleaned_text → prompt 含 (正文为空)。"""
    model = FakeChatModel(output=_success_output())
    result = await _analyze_node(_base_state(cleaned_text=""), chat_model=model)
    assert result.get("primary_intent") == "cancel_order"


# ---------------------------------------------------------------------------
# 完整图测试
# ---------------------------------------------------------------------------


async def test_compiled_graph_normal_path() -> None:
    """端到端跑编译后的图，验证 analyze 节点正确执行。"""
    model = FakeChatModel(output=_success_output())
    graph = build_email_analysis_graph(model)

    result = await graph.ainvoke(_base_state())

    assert result["primary_intent"] == "cancel_order"
    assert result["llm_model"] == "fake-model"
    assert "error" not in result


async def test_compiled_graph_llm_error_path() -> None:
    """端到端：LLM 失败（重试耗尽）→ 异常穿透 ainvoke，chain_error 进入追踪。"""
    handler = GraphTraceHandler()
    model = FakeChatModel(error=RuntimeError("timeout"))
    graph = build_email_analysis_graph(model)

    with pytest.raises(LLMInvocationError) as exc_info:
        await graph.ainvoke(_base_state(), config={"callbacks": [handler]})

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert any(e["type"] == "chain_error" for e in handler.dump())


async def test_compiled_graph_retries_then_succeeds() -> None:
    """RetryPolicy：首次 LLM 调用失败自动重试一次，第二次成功则正常返回。"""
    model = FakeChatModel(output=_success_output(), flaky=RuntimeError("transient"))
    graph = build_email_analysis_graph(model)

    result = await graph.ainvoke(_base_state())

    assert result["primary_intent"] == "cancel_order"


# ---------------------------------------------------------------------------
# 调用链追踪测试（GraphTraceHandler）
# ---------------------------------------------------------------------------


class _FakeResponse:
    """模拟 LLM 响应，仅带 usage_metadata 属性。"""

    usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_trace_handler_llm_events_with_usage() -> None:
    handler = GraphTraceHandler()
    run_id = uuid4()
    handler.on_chat_model_start(None, [], run_id=run_id, name="fake-model")
    handler.on_llm_end(_FakeResponse(), run_id=run_id)

    events = handler.dump()
    assert [e["type"] for e in events] == ["llm_start", "llm_end"]
    assert events[0]["name"] == "fake-model"
    assert events[1]["usage"]["total_tokens"] == 15
    assert events[1]["elapsed_ms"] >= 0


def test_trace_handler_chain_error_event() -> None:
    handler = GraphTraceHandler()
    run_id = uuid4()
    handler.on_chain_start(None, {}, run_id=run_id, name="analyze")
    handler.on_chain_error(RuntimeError("boom"), run_id=run_id)

    events = handler.dump()
    assert events[-1]["type"] == "chain_error"
    assert "RuntimeError" in events[-1]["error"]
    assert events[-1]["elapsed_ms"] >= 0


def test_trace_handler_dump_returns_copy() -> None:
    handler = GraphTraceHandler()
    handler.on_chain_start(None, {}, run_id=uuid4(), name="analyze")
    events = handler.dump()
    events.clear()
    assert handler.dump()  # dump 是副本，外部修改不影响内部状态


async def test_compiled_graph_captures_trace_events() -> None:
    """带 callbacks 跑编译后的图，捕获 graph/analyze 节点链路且父子关联完整。"""
    handler = GraphTraceHandler()
    model = FakeChatModel(output=_success_output())
    graph = build_email_analysis_graph(model)

    result = await graph.ainvoke(_base_state(), config={"callbacks": [handler]})

    assert "error" not in result
    events = handler.dump()
    starts = [e for e in events if e["type"] == "chain_start"]
    assert any(e["name"] == "analyze" for e in starts)
    # 父子关联：至少一个事件挂在上文出现过的 run_id 之下
    run_ids = {e["run_id"] for e in events}
    assert any(e.get("parent_run_id") in run_ids for e in events)
    assert all(e["type"] != "chain_error" for e in events)


async def test_compiled_graph_propagates_config_to_llm() -> None:
    """config 注入节点并穿透到内层 runnable，否则 LLM 事件无法进入调用树。"""
    model = FakeChatModel(output=_success_output())
    graph = build_email_analysis_graph(model)
    handler = GraphTraceHandler()

    await graph.ainvoke(_base_state(), config={"callbacks": [handler]})

    assert model.received_config is not None
    assert "callbacks" in model.received_config


async def test_handler_emits_structlog_events() -> None:
    """handler 在收集事件的同时实时发出 structlog 日志（含 usage、prompt 长度与异常）。"""
    handler = GraphTraceHandler()
    llm_run = uuid4()
    chain_run = uuid4()

    with capture_logs() as cap:
        handler.on_chat_model_start(
            None, [[HumanMessage(content="hello")]], run_id=llm_run, name="fake-model"
        )
        handler.on_llm_end(_FakeResponse(), run_id=llm_run)
        handler.on_chain_error(RuntimeError("boom"), run_id=chain_run)

    assert {"llm_call_start", "llm_call_done", "graph_chain_error"} <= {
        e["event"] for e in cap
    }

    start = next(e for e in cap if e["event"] == "llm_call_start")
    assert start["prompt_chars"] == len("hello")

    done = next(e for e in cap if e["event"] == "llm_call_done")
    assert done["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert done["elapsed_ms"] >= 0

    err = next(e for e in cap if e["event"] == "graph_chain_error")
    assert isinstance(err["exc_info"], RuntimeError)


async def test_contextvars_flow_into_handler_logs(monkeypatch) -> None:
    """业务上下文（bound_contextvars）经 merge_contextvars 合并进 handler 发出的日志。"""
    captured: list[dict[str, Any]] = []

    def _capture(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        captured.append(event_dict)
        return event_dict

    # 直接给 handler 注入带 merge_contextvars 的独立 logger，
    # 绕开全局 structlog 配置与 cache_logger_on_first_use 缓存
    test_logger = structlog.wrap_logger(
        structlog.ReturnLogger(),
        processors=[structlog.contextvars.merge_contextvars, _capture],
    )
    monkeypatch.setattr(trace_handle, "logger", test_logger)

    with structlog.contextvars.bound_contextvars(email_id=7):
        model = FakeChatModel(output=_success_output())
        graph = build_email_analysis_graph(model)
        await graph.ainvoke(_base_state(), config={"callbacks": [GraphTraceHandler()]})

    # FakeChatModel 非 langchain Runnable，不触发 LLM 级回调；chain 事件由 langgraph 发出
    starts = [e for e in captured if e.get("event") == "graph_chain_start"]
    assert starts and starts[0]["email_id"] == 7
