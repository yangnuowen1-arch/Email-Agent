"""单邮件意向分析 LangGraph：analyze → (条件边) → detect_and_translate → END。

从 coordinator 拿到清洗后的正文后进入本图：analyze 在原文上完成结构化
意向分析，随后按主意图条件路由——垃圾/通知类（TRANSLATION_EXCLUDED_INTENTS）
直接结束，其余邮件进入 detect_and_translate 节点（检测语言并翻译为中文，
译文仅落库展示，不回灌 analyze）。

构建方式：
    graph = build_email_analysis_graph(chat_model)
    result = await graph.ainvoke(initial_state)

调用链追踪与日志（handler 见 app.agent.trace_handle，实时输出结构化日志并收集事件）：
    handler = GraphTraceHandler()
    result = await graph.ainvoke(initial_state, config={"callbacks": [handler]})

错误处理：LLM 调用失败统一抛 ``LLMInvocationError``，原样穿透 ``ainvoke``
（LangGraph 不包装节点异常，异常链保留在 ``__cause__``），由调用方
``except AnalysisGraphError`` 捕获；节点其余逻辑错误不捕获、原样传播。
LLMInvocationError 由节点级 RetryPolicy 自动重试一次。

日志全部由 GraphTraceHandler 回调实时输出（节点自身零日志），业务上下文
（email_id / account_id）由调用方经 ``structlog.contextvars`` 注入。

依赖全部闭包注入，无模块级全局，不触碰 DB。
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Optional, TypedDict

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy
from pydantic import ValidationError

from app.agent.constants import (
    ANALYSIS_MAX_BODY_CHARS,
    HAN_RANGE,
    HANGUL_RANGE,
    KANA_RANGE,
    LLM_CALL_TIMEOUT_SECONDS,
    LLM_NODE_MAX_ATTEMPTS,
)
from app.agent.errors import LLMInvocationError
from app.agent.prompts import EMAIL_ANALYSIS_SYSTEM_PROMPT, EMAIL_TRANSLATE_SYSTEM_PROMPT
from app.schemas.analysis import (
    TRANSLATION_EXCLUDED_INTENTS,
    EmailAnalysisOutput,
    EmailTranslationOutput,
)
from app.services.preprocess import compose_email_view

# ---------------------------------------------------------------------------
# 状态定义
# ---------------------------------------------------------------------------


class EmailAnalysisState(TypedDict, total=False):
    """单邮件分析节点间传递的状态。"""

    email_id: int
    account_id: int
    subject: str
    sender: str | None
    sent_at: Any  # datetime | None，避免 import 循环

    # coordinator 清洗后传入
    cleaned_text: str

    # detect_and_translate 节点产出（译文仅落库展示，不回灌 analyze）
    source_language: str
    translated_subject: str
    translated_text: str

    # analyze 节点产出
    primary_intent: str
    intents: list[dict]
    reasoning_summary: str
    entities: dict
    sentiment: str
    priority: str
    suggested_tools: list[str]
    llm_model: str


async def _analyze_node(
    state: EmailAnalysisState,
    # langgraph 按注解字面匹配来注入 config，仅认 RunnableConfig/Optional[RunnableConfig]，
    # 不识别 PEP 604 的 `X | None`（本文件启用 future annotations 后注解为字符串，须用 Optional）
    config: Optional[RunnableConfig] = None,  # noqa: UP045
    *,
    chat_model: Any,
    max_body_chars: int = ANALYSIS_MAX_BODY_CHARS,
) -> dict:
    """调用 LLM 执行结构化意向分析。

    langgraph 会把运行时 config 注入到名为 ``config`` 的参数中；
    节点内必须把 config 继续传给内层 runnable，LLM 调用才会进入调用链追踪。

    LLM 调用失败（超时 / 网络 / 解析失败等）统一抛 ``LLMInvocationError``，
    由节点级 RetryPolicy 重试一次、重试耗尽后穿透 ``ainvoke``；
    节点其余逻辑（清洗视图组装等）的错误不捕获，原样传播以暴露代码 bug。
    节点自身零日志：开始 / 耗时 / usage / 错误均由 GraphTraceHandler 回调输出。
    """
    cleaned = state.get("cleaned_text", "")
    view = compose_email_view(
        subject=state.get("subject", ""),
        sender=state.get("sender"),
        sent_at=state.get("sent_at"),
        cleaned_text=cleaned[:max_body_chars],
    )

    try:
        structured = chat_model.with_structured_output(
            EmailAnalysisOutput, method="function_calling"
        )
        result = await asyncio.wait_for(
            structured.ainvoke(
                [
                    SystemMessage(content=EMAIL_ANALYSIS_SYSTEM_PROMPT),
                    HumanMessage(content=view),
                ],
                config=config,
            ),
            timeout=LLM_CALL_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise LLMInvocationError(f"LLM 调用超时（{LLM_CALL_TIMEOUT_SECONDS}s）") from exc
    except (OutputParserException, ValidationError) as exc:
        raise LLMInvocationError(f"LLM 输出解析失败: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LLMInvocationError(f"{type(exc).__name__}: {exc}") from exc

    if result is None:
        raise LLMInvocationError("LLM returned None")

    return {
        "primary_intent": result.primary_intent,
        "intents": [i.model_dump() for i in result.intents],
        "reasoning_summary": result.reasoning_summary,
        "entities": result.entities,
        "sentiment": result.sentiment,
        "priority": result.priority,
        "suggested_tools": result.suggested_tools,
        "llm_model": getattr(chat_model, "model_name", "unknown"),
    }


def _is_chinese_dominant_text(text: str) -> bool:
    """逐字符统计判定文本是否中文主导，命中则检测翻译节点跳过 LLM 调用。

    含假名/谚文必非中文；无汉字必非中文；拉丁字母多于汉字按外文处理。
    拿不准返回 False 交给 LLM 判定，宁可多花一次调用不误跳。
    """
    han = latin = 0
    for ch in text:
        code = ord(ch)
        if KANA_RANGE[0] <= code <= KANA_RANGE[1] or HANGUL_RANGE[0] <= code <= HANGUL_RANGE[1]:
            return False
        if HAN_RANGE[0] <= code <= HAN_RANGE[1]:
            han += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    return han > 0 and han >= latin


async def _detect_language_and_translate_node(
    state: EmailAnalysisState,
    # langgraph 按注解字面匹配来注入 config，仅认 RunnableConfig/Optional[RunnableConfig]，
    # 不识别 PEP 604 的 `X | None`（本文件启用 future annotations 后注解为字符串，须用 Optional）
    config: Optional[RunnableConfig] = None,  # noqa: UP045
    *,
    chat_model: Any,
    max_body_chars: int = ANALYSIS_MAX_BODY_CHARS,
) -> dict:
    """检测邮件语言并将主题/正文翻译为简体中文（单次 LLM 调用），产出落库展示用译文。

    短路顺序：空内容 → unknown；中文主导 → zh；其余才调 LLM。
    异常处理与 analyze 一致：统一包装 ``LLMInvocationError``，由节点级 RetryPolicy
    重试一次。节点自身零日志，config 须透传内层 runnable 保证进入调用链追踪。
    """
    subject = state.get("subject", "")
    cleaned = state.get("cleaned_text", "")

    if not subject.strip() and not cleaned.strip():
        return {"source_language": "unknown"}
    if _is_chinese_dominant_text(f"{subject}\n{cleaned}"):
        return {"source_language": "zh"}

    view = compose_email_view(
        subject=subject,
        sender=state.get("sender"),
        sent_at=state.get("sent_at"),
        cleaned_text=cleaned[:max_body_chars],
    )

    try:
        structured = chat_model.with_structured_output(
            EmailTranslationOutput, method="function_calling"
        )
        result = await asyncio.wait_for(
            structured.ainvoke(
                [
                    SystemMessage(content=EMAIL_TRANSLATE_SYSTEM_PROMPT),
                    HumanMessage(content=view),
                ],
                config=config,
            ),
            timeout=LLM_CALL_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise LLMInvocationError(f"LLM 调用超时（{LLM_CALL_TIMEOUT_SECONDS}s）") from exc
    except (OutputParserException, ValidationError) as exc:
        raise LLMInvocationError(f"LLM 输出解析失败: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LLMInvocationError(f"{type(exc).__name__}: {exc}") from exc

    if result is None:
        raise LLMInvocationError("LLM returned None")

    return {
        "source_language": result.detected_language,
        "translated_subject": result.translated_subject,
        "translated_text": result.translated_text[:max_body_chars],
    }


def _route_translation_by_primary_intent(state: EmailAnalysisState) -> str:
    """按 analyze 产出的主意图路由：垃圾/通知类直接结束，其余进检测翻译节点。"""
    if state.get("primary_intent") in TRANSLATION_EXCLUDED_INTENTS:
        return END
    return "detect_and_translate"


def build_email_analysis_graph(chat_model: Any):
    """构建单邮件意向分析图，依赖全部闭包注入。

    调用链追踪：ainvoke 时传 ``config={"callbacks": [GraphTraceHandler()]}``，
    结束后由 ``handler.dump()`` 取事件列表（用法见模块 docstring）。

    analyze 与 detect_and_translate 节点各挂 RetryPolicy：仅对 LLMInvocationError
    重试（次数见 ``constants.LLM_NODE_MAX_ATTEMPTS``，含首次），重试耗尽后异常
    原样抛给 ainvoke 调用方。
    analyze 之后按主意图条件路由：垃圾/通知类（TRANSLATION_EXCLUDED_INTENTS）
    直接结束，其余进入 detect_and_translate。
    """

    builder = StateGraph(EmailAnalysisState)

    builder.add_node(
        "analyze",
        partial(_analyze_node, chat_model=chat_model),
        retry_policy=RetryPolicy(
            max_attempts=LLM_NODE_MAX_ATTEMPTS, retry_on=(LLMInvocationError,)
        ),
    )
    builder.add_node(
        "detect_and_translate",
        partial(_detect_language_and_translate_node, chat_model=chat_model),
        retry_policy=RetryPolicy(
            max_attempts=LLM_NODE_MAX_ATTEMPTS, retry_on=(LLMInvocationError,)
        ),
    )

    builder.add_conditional_edges(
        "analyze",
        _route_translation_by_primary_intent,
        {"detect_and_translate": "detect_and_translate", END: END},
    )
    builder.add_edge("detect_and_translate", END)

    builder.set_entry_point("analyze")

    return builder.compile()
