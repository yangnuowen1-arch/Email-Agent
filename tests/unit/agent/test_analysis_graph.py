"""单邮件意向分析图单元测试：analyze 节点 + 编译后图端到端。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import BaseMessage

from app.agent.analysis_graph import (
    EmailAnalysisState,
    _analyze_node,
    build_email_analysis_graph,
)
from app.schemas.analysis import EmailAnalysisOutput, IntentDetail

# ---------------------------------------------------------------------------
# FakeChatModel（鸭子类型，模拟 with_structured_output）
# ---------------------------------------------------------------------------


class _FakeStructuredRunnable:
    def __init__(self, result: Any) -> None:
        self._result = result

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> Any:
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

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeStructuredRunnable:
        if self._error:
            return _FakeStructuredRunnable(self._error)
        return _FakeStructuredRunnable(self._output)


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
