"""单邮件意向分析 LangGraph：analyze → END。

从 coordinator 拿到清洗后的正文后进入本图，完成：
结构化 LLM 意向分析。

构建方式：
    graph = build_email_analysis_graph(chat_model)
    result = await graph.ainvoke(initial_state)

依赖全部闭包注入，无模块级全局，不触碰 DB。
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from app.agent.prompts import EMAIL_ANALYSIS_SYSTEM_PROMPT
from app.schemas.analysis import EmailAnalysisOutput
from app.services.preprocess import compose_email_view

logger = logging.getLogger(__name__)


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

    # analyze 节点产出
    primary_intent: str
    intents: list[dict]
    reasoning_summary: str
    entities: dict
    sentiment: str
    priority: str
    suggested_tools: list[str]
    llm_model: str

    # 错误标记（analyze 失败时设置）
    error: str | None


async def _analyze_node(
    state: EmailAnalysisState,
    *,
    chat_model: Any,
    max_body_chars: int = 6000,
) -> dict:
    """调用 LLM 执行结构化意向分析。"""
    if state.get("error"):
        return {}

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
                ]
            ),
            timeout=120,
        )

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

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "llm_analysis_failed",
            extra={"email_id": state.get("email_id"), "error": f"{type(exc).__name__}: {exc}"},
        )
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_email_analysis_graph(chat_model: Any):
    """构建单邮件意向分析图，依赖全部闭包注入。"""

    builder = StateGraph(EmailAnalysisState)

    builder.add_node("analyze", partial(_analyze_node, chat_model=chat_model))

    builder.add_edge("analyze", END)

    builder.set_entry_point("analyze")

    return builder.compile()
