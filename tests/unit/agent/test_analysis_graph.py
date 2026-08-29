"""单邮件意向分析图单元测试：analyze 节点 + 编译后图端到端 + 调用链追踪。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langchain_core.messages import BaseMessage

from app.agent.analysis_graph import (
    EmailAnalysisState,
    GraphTraceHandler,
    _analyze_node,
    build_email_analysis_graph,
)
from app.schemas.analysis import EmailAnalysisOutput, IntentDetail

# ---------------------------------------------------------------------------
# FakeChatModel（鸭子类型，模拟 with_structured_output）
# ---------------------------------------------------------------------------


class _FakeStructuredRunnable:
    def __init__(self, result: Any, model: FakeChatModel) -> None:
        self._result = result
        self._model = model

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> Any:
        self._model.received_config = kwargs.get("config")
        if isinstance(self._result, Exception):
            raise self._result
        if isinstance(self._result, EmailAnalysisOutput):
            return self._result
        return EmailAnalysisOutput.model_validate(self._result)


class FakeChatModel:
    def __init__(self, *, output: Any = None, error: Exception | None = None):
        self._output = output
        self._error = error
        self.model_name = "fake-model"
        self.received_config: Any = None

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeStructuredRunnable:
        if self._error:
            return _FakeStructuredRunnable(self._error, self)
        return _FakeStructuredRunnable(self._output, self)


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
    result = await _analyze_node(_base_state(), chat_model=model)

    assert result.get("error") is not None
    assert "RuntimeError" in result["error"]


async def test_analyze_node_invalid_output() -> None:
    """模型返回非法值（intents=[]）导致 Pydantic 校验失败。"""
    model = FakeChatModel(output={"primary_intent": "x", "intents": [], "reasoning_summary": "t"})
    result = await _analyze_node(_base_state(), chat_model=model)

    assert result.get("error") is not None


async def test_analyze_node_out_of_whitelist_intent_falls_back() -> None:
    """LLM 返回白名单外意图 → Pydantic 校验失败 → error。"""
    model = FakeChatModel(
        output={
            "primary_intent": "resume_request",
            "intents": [{"category": "resume_request", "confidence": 0.9, "reasoning": "新场景"}],
            "reasoning_summary": "t",
        }
    )
    result = await _analyze_node(_base_state(), chat_model=model)

    assert result.get("error") is not None


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
    """端到端：LLM 失败 → error 字段有值。"""
    model = FakeChatModel(error=RuntimeError("timeout"))
    graph = build_email_analysis_graph(model)

    result = await graph.ainvoke(_base_state())

    assert result.get("error") is not None
    assert "RuntimeError" in result["error"]


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
