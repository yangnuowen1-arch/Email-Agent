"""单邮件意向分析图单元测试：analyze 节点 + 编译后图端到端 + 调用链追踪。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import structlog
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import END
from structlog.testing import capture_logs

from app.agent import trace_handle
from app.agent.analysis_graph import (
    EmailAnalysisState,
    _analyze_node,
    _detect_language_and_translate_node,
    _route_translation_by_primary_intent,
    build_email_analysis_graph,
)
from app.agent.errors import LLMInvocationError
from app.agent.trace_handle import GraphTraceHandler
from app.schemas.analysis import EmailAnalysisOutput, EmailTranslationOutput, IntentDetail

# ---------------------------------------------------------------------------
# FakeChatModel（鸭子类型，模拟 with_structured_output）
# ---------------------------------------------------------------------------


class _FakeStructuredRunnable:
    """按构造时传入的 schema 校验 outcomes：Exception 则抛出，其余经 schema 校验返回。

    outcomes 只剩一个时重复它（RetryPolicy 重试且无后续结果时保持同样表现）。
    每次调用向 model.calls 记录 (schema 类名, 收到的 messages)，供断言调用次数与内容。
    """

    def __init__(self, outcomes: list[Any], schema: Any, model: FakeChatModel) -> None:
        self._outcomes = outcomes
        self._schema = schema
        self._model = model

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> Any:
        self._model.received_config = kwargs.get("config")
        self._model.calls.append((self._schema.__name__, messages))
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, self._schema):
            return outcome
        return self._schema.model_validate(outcome)


class FakeChatModel:
    def __init__(
        self,
        *,
        output: Any = None,
        error: Exception | None = None,
        flaky: Exception | None = None,
        outcomes: list[Any] | None = None,
    ):
        """``flaky``：首次调用抛该异常，之后按 output/error 表现（验证 RetryPolicy）。

        ``outcomes``：多节点图的整段调用队列（如 [分析输出, 翻译输出]），
        按节点执行顺序依次消费；未提供时按 output/error/flaky 组装单节点表现。
        """
        if outcomes is None:
            outcomes = []
            if flaky is not None:
                outcomes.append(flaky)
            outcomes.append(error if error is not None else output)
        self._outcomes = outcomes
        self.model_name = "fake-model"
        self.received_config: Any = None
        self.calls: list[tuple[str, list[BaseMessage]]] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeStructuredRunnable:
        return _FakeStructuredRunnable(self._outcomes, schema, self)


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


def _analysis_success_output(intent: str = "cancel_order") -> EmailAnalysisOutput:
    return EmailAnalysisOutput(
        primary_intent=intent,
        intents=[
            IntentDetail(
                category=intent,
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


def _translation_success_output() -> EmailTranslationOutput:
    return EmailTranslationOutput(
        detected_language="en",
        translated_subject="取消订单 ORD-123 的请求",
        translated_text="请帮我取消订单 ORD-123",
    )


# ---------------------------------------------------------------------------
# analyze_node 测试
# ---------------------------------------------------------------------------


async def test_analyze_node_normal() -> None:
    model = FakeChatModel(output=_analysis_success_output())
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
    model = FakeChatModel(output=_analysis_success_output())
    result = await _analyze_node(_base_state(cleaned_text=""), chat_model=model)
    assert result.get("primary_intent") == "cancel_order"


# ---------------------------------------------------------------------------
# detect_and_translate_node 测试
# ---------------------------------------------------------------------------


async def test_detect_and_translate_node_translates_foreign_email() -> None:
    """外文邮件 → 调 LLM 检测+翻译，产出语言码与中文译文。"""
    model = FakeChatModel(output=_translation_success_output())
    state = _base_state(subject="Please cancel order ORD-123", cleaned_text="I want a refund")
    result = await _detect_language_and_translate_node(state, chat_model=model)

    assert result["source_language"] == "en"
    assert result["translated_subject"] == "取消订单 ORD-123 的请求"
    assert result["translated_text"] == "请帮我取消订单 ORD-123"
    assert model.calls[0][0] == "EmailTranslationOutput"


async def test_detect_and_translate_node_skips_llm_for_chinese_text() -> None:
    """中文邮件 → 启发式命中，零 LLM 调用，仅标记源语言 zh。"""
    model = FakeChatModel(output=_translation_success_output())
    result = await _detect_language_and_translate_node(_base_state(), chat_model=model)

    assert result == {"source_language": "zh"}
    assert model.calls == []


async def test_detect_and_translate_node_returns_unknown_for_empty_content() -> None:
    """主题与正文均为空 → 无内容可翻译，不调 LLM，标记 unknown。"""
    model = FakeChatModel(output=_translation_success_output())
    result = await _detect_language_and_translate_node(
        _base_state(subject="", cleaned_text=""), chat_model=model
    )

    assert result == {"source_language": "unknown"}
    assert model.calls == []


async def test_detect_and_translate_node_wraps_llm_error() -> None:
    """LLM 调用失败 → 包装为 LLMInvocationError，异常链保留原始异常。"""
    model = FakeChatModel(error=RuntimeError("LLM timeout"))
    state = _base_state(subject="Please cancel order ORD-123")
    with pytest.raises(LLMInvocationError) as exc_info:
        await _detect_language_and_translate_node(state, chat_model=model)

    assert isinstance(exc_info.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# 翻译路由测试（analyze 后的条件边）
# ---------------------------------------------------------------------------


def test_route_translation_by_primary_intent_spam_ends() -> None:
    """垃圾/通知类意图 → 直接结束，不进检测翻译节点。"""
    assert (
        _route_translation_by_primary_intent({**_base_state(), "primary_intent": "spam_or_notice"})
        == END
    )


def test_route_translation_by_primary_intent_business_enters_detect_and_translate() -> None:
    """业务意图（售前售后）→ 进检测翻译节点。"""
    assert (
        _route_translation_by_primary_intent({**_base_state(), "primary_intent": "refund_request"})
        == "detect_and_translate"
    )


# ---------------------------------------------------------------------------
# 完整图测试
# ---------------------------------------------------------------------------


async def test_compiled_graph_normal_path() -> None:
    """端到端跑编译后的图，验证 analyze 节点正确执行。"""
    model = FakeChatModel(output=_analysis_success_output())
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
    model = FakeChatModel(output=_analysis_success_output(), flaky=RuntimeError("transient"))
    graph = build_email_analysis_graph(model)

    result = await graph.ainvoke(_base_state())

    assert result["primary_intent"] == "cancel_order"


async def test_compiled_graph_translates_foreign_business_email() -> None:
    """外文业务邮件全流程：analyze（原文）→ detect_and_translate，两次调用、译文入终态。"""
    model = FakeChatModel(outcomes=[_analysis_success_output(), _translation_success_output()])
    graph = build_email_analysis_graph(model)
    state = _base_state(subject="Please cancel order ORD-123", cleaned_text="I want a refund")

    result = await graph.ainvoke(state)

    assert result["primary_intent"] == "cancel_order"
    assert result["source_language"] == "en"
    assert result["translated_subject"] == "取消订单 ORD-123 的请求"
    assert result["translated_text"] == "请帮我取消订单 ORD-123"
    assert [name for name, _ in model.calls] == [
        "EmailAnalysisOutput",
        "EmailTranslationOutput",
    ]


async def test_compiled_graph_skips_detect_and_translate_for_spam() -> None:
    """垃圾邮件：只花 1 次 analyze 调用，不进检测翻译节点，无译文产出。"""
    model = FakeChatModel(output=_analysis_success_output(intent="spam_or_notice"))
    graph = build_email_analysis_graph(model)
    state = _base_state(subject="You won a prize!", cleaned_text="Click here now")

    result = await graph.ainvoke(state)

    assert result["primary_intent"] == "spam_or_notice"
    assert "source_language" not in result
    assert "translated_subject" not in result
    assert "translated_text" not in result
    assert len(model.calls) == 1


async def test_compiled_graph_chinese_email_needs_only_one_llm_call() -> None:
    """中文业务邮件：路由进检测翻译节点但启发式短路，全程仅 1 次 LLM 调用。"""
    model = FakeChatModel(output=_analysis_success_output())
    graph = build_email_analysis_graph(model)

    result = await graph.ainvoke(_base_state())

    assert result["primary_intent"] == "cancel_order"
    assert result["source_language"] == "zh"
    assert [name for name, _ in model.calls] == ["EmailAnalysisOutput"]


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
    model = FakeChatModel(output=_analysis_success_output())
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
    model = FakeChatModel(output=_analysis_success_output())
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

    assert {"llm_call_start", "llm_call_done", "graph_chain_error"} <= {e["event"] for e in cap}

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
        model = FakeChatModel(output=_analysis_success_output())
        graph = build_email_analysis_graph(model)
        await graph.ainvoke(_base_state(), config={"callbacks": [GraphTraceHandler()]})

    # FakeChatModel 非 langchain Runnable，不触发 LLM 级回调；chain 事件由 langgraph 发出
    starts = [e for e in captured if e.get("event") == "graph_chain_start"]
    assert starts and starts[0]["email_id"] == 7


# ---------------------------------------------------------------------------
# 附件参与分析测试（分层视图 / 缓存复用 / 视觉降级）
# ---------------------------------------------------------------------------


class FakeVisionModel:
    """记录调用的假视觉模型；calls 为收到的消息列表，行为可注入。"""

    def __init__(self, *, reply: str = "发票照片", error: Exception | None = None):
        self._reply = reply
        self._error = error
        self.calls: list[list] = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        from types import SimpleNamespace

        return SimpleNamespace(content=self._reply)


def _eml_bytes(subject: str, body: str) -> bytes:
    return (
        b"From: Old <old@example.com>\r\n"
        b"Subject: " + subject.encode() + b"\r\n"
        b"\r\n" + body.encode() + b"\r\n"
    )


def _eml_attachment_state(**overrides: Any) -> EmailAnalysisState:
    attachment = {
        "attachment_id": 9,
        "kind": "email",
        "filename": "forwarded.eml",
        "content_type": "message/rfc822",
        "content": _eml_bytes("Old thread", "old body text"),
        "extracted_text": None,
    }
    return _base_state(**{"attachments": [attachment], **overrides})


async def test_analyze_node_eml_attachment_composes_layered_view() -> None:
    """.eml 附件被解析进分层视图，提取结果进入待写回缓存。"""
    model = FakeChatModel(output=_analysis_success_output())

    result = await _analyze_node(_eml_attachment_state(), chat_model=model)

    assert len(result["attachment_views"]) == 1
    view = result["attachment_views"][0]
    assert view["kind"] == "email"
    assert view["filename"] == "forwarded.eml"
    assert "Old thread" in view["text"]
    assert "old body text" in view["text"]
    assert result["extracted_cache"] == [{"attachment_id": 9, "extracted_text": view["text"]}]
    # LLM 收到的 HumanMessage 含转发邮件分层段
    human = model.calls[0][1][1].content
    assert "--- 转发邮件（附件：forwarded.eml）---" in human


async def test_analyze_node_cached_extracted_text_reused_without_content() -> None:
    """已有提取缓存：不携带字节也直接进视图（重复分析不重复拉取/调用）。"""
    attachment = {
        "attachment_id": 7,
        "kind": "image",
        "filename": "cached.png",
        "content_type": "image/png",
        "content": None,
        "extracted_text": "上次识别的发票内容",
    }
    model = FakeChatModel(output=_analysis_success_output())
    vision = FakeVisionModel()

    result = await _analyze_node(
        _base_state(cleaned_text="见附件", attachments=[attachment]),
        chat_model=model,
        vision_model=vision,
    )

    assert result["attachment_views"][0]["text"] == "上次识别的发票内容"
    assert result["extracted_cache"][0]["attachment_id"] == 7
    assert vision.calls == []


async def test_analyze_node_image_short_shell_calls_vision() -> None:
    """壳层极短（见附件）+ 图片附件 → 调视觉识别进视图。"""
    attachment = {
        "attachment_id": 5,
        "kind": "image",
        "filename": "invoice.png",
        "content_type": "image/png",
        "content": b"png-bytes",
        "extracted_text": None,
    }
    model = FakeChatModel(output=_analysis_success_output())
    vision = FakeVisionModel(reply="发票照片，金额 128.00")

    result = await _analyze_node(
        _base_state(cleaned_text="见附件", attachments=[attachment]),
        chat_model=model,
        vision_model=vision,
    )

    assert len(vision.calls) == 1
    assert result["attachment_views"][0]["text"] == "发票照片，金额 128.00"
    assert "--- 图片内容（附件：invoice.png，视觉识别）---" in model.calls[0][1][1].content


async def test_analyze_node_image_rich_shell_skips_vision() -> None:
    """壳层正文足够长 → 不做视觉识别（成本控制），无图片视图。"""
    attachment = {
        "attachment_id": 5,
        "kind": "image",
        "filename": "invoice.png",
        "content_type": "image/png",
        "content": b"png-bytes",
        "extracted_text": None,
    }
    model = FakeChatModel(output=_analysis_success_output())
    vision = FakeVisionModel()

    result = await _analyze_node(
        _base_state(cleaned_text="请帮我处理这个订单" * 30, attachments=[attachment]),
        chat_model=model,
        vision_model=vision,
    )

    assert vision.calls == []
    assert result["attachment_views"] == []
    assert result["extracted_cache"] == []


async def test_analyze_node_image_budget_capped() -> None:
    """每封邮件最多识别 3 张图片，超出部分跳过。"""
    attachments = [
        {
            "attachment_id": i,
            "kind": "image",
            "filename": f"img{i}.png",
            "content_type": "image/png",
            "content": b"png",
            "extracted_text": None,
        }
        for i in range(5)
    ]
    model = FakeChatModel(output=_analysis_success_output())
    vision = FakeVisionModel()

    result = await _analyze_node(
        _base_state(cleaned_text="见附件", attachments=attachments),
        chat_model=model,
        vision_model=vision,
    )

    assert len(vision.calls) == 3
    assert len(result["attachment_views"]) == 3


async def test_analyze_node_vision_failure_degrades() -> None:
    """视觉模型调用失败 → 图片段降级跳过，分析正常完成。"""
    attachment = {
        "attachment_id": 5,
        "kind": "image",
        "filename": "invoice.png",
        "content_type": "image/png",
        "content": b"png-bytes",
        "extracted_text": None,
    }
    model = FakeChatModel(output=_analysis_success_output())
    vision = FakeVisionModel(error=RuntimeError("vision down"))

    result = await _analyze_node(
        _base_state(cleaned_text="见附件", attachments=[attachment]),
        chat_model=model,
        vision_model=vision,
    )

    assert result["attachment_views"] == []
    assert result["extracted_cache"] == []
    assert result["primary_intent"] == "cancel_order"


async def test_detect_and_translate_uses_attachment_text_for_language_check() -> None:
    """中文壳层 + 英文转发邮件 → 不误判 zh 短路，进 LLM 翻译。"""
    model = FakeChatModel(output=_translation_success_output())
    state = _eml_attachment_state(
        subject="请处理",
        cleaned_text="见附件",
    )
    # 覆盖附件内容为英文转发邮件
    state["attachments"] = [
        {
            "attachment_id": 9,
            "kind": "email",
            "filename": "forwarded.eml",
            "content_type": "message/rfc822",
            "content": _eml_bytes("Please cancel order ORD-123", "I want a refund"),
            "extracted_text": None,
        }
    ]
    # 预置 analyze 产出的分层视图（翻译节点从 state 读取）
    state["attachment_views"] = [
        {
            "kind": "email",
            "filename": "forwarded.eml",
            "text": "Subject: Please cancel order ORD-123",
        }
    ]

    result = await _detect_language_and_translate_node(state, chat_model=model)

    assert result["source_language"] == "en"
    assert model.calls[0][0] == "EmailTranslationOutput"


async def test_build_graph_accepts_vision_model() -> None:
    """build_email_analysis_graph 支持 vision_model 注入，图正常执行。"""
    model = FakeChatModel(outcomes=[_analysis_success_output(), _translation_success_output()])
    graph = build_email_analysis_graph(model, vision_model=FakeVisionModel())

    result = await graph.ainvoke(_eml_attachment_state())

    assert result["primary_intent"] == "cancel_order"
    assert result["attachment_views"]
