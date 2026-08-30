"""单邮件意向分析 LangGraph：analyze → (条件边) → detect_and_translate → (条件边) → 草稿分支 → END。

从 coordinator 拿到清洗后的正文与附件数据后进入本图：analyze 先做附件内容
提取（.eml 附件递归解析、图片视觉识别——均在本节点内完成，不新增节点），
组装分层视图后在原文上完成结构化意向分析；随后按主意图条件路由——
垃圾/通知类（TRANSLATION_EXCLUDED_INTENTS）直接结束，其余邮件进入
detect_and_translate 节点（检测语言并翻译为中文，译文仅落库展示，不回灌 analyze）；
翻译结束后第二次条件路由：主意图命中 ``DRAFT_CATEGORY_BY_INTENT``（售前/售后
咨询类）且注入了 KnowledgeRetriever 时，进入 draft_presale / draft_aftersale
节点——检索知识库（售前查 faq、售后查 sop），质量门槛通过后起草回复草稿。
草稿只写进 state 由 coordinator 落 email_drafts 待人工确认，本图不发送邮件。

附件数据流：coordinator 查询附件（含 COS 拉取的字节或已有提取缓存）放入
初始 state 的 ``attachments``；analyze 提取后产出 ``attachment_views``（分层
视图段）与 ``extracted_cache``（供 coordinator 写回缓存）及
``intent_evidence_source``（意图证据来源）。本图自身不触 DB；唯一的外部 IO
是草稿节点经闭包注入的 KnowledgeRetriever（向量检索）。

检索观测数据流：草稿节点检索发生即把实际执行的 query 与原始命中写入
``retrieval_query`` / ``retrieved_chunks``（含质量门槛未通过、生成失败路径），
仅供调用方日志排查"为什么没出草稿"，不落库——落库的核对依据仍是
``draft_sources``；检索前早退（意图类别不匹配 / 空 query）两键缺席。

构建方式：
    graph = build_email_analysis_graph(chat_model, vision_model=None, knowledge_retriever=None)
    result = await graph.ainvoke(initial_state)

调用链追踪与日志（handler 见 app.agent.trace_handle，实时输出结构化日志并收集事件）：
    handler = GraphTraceHandler()
    result = await graph.ainvoke(initial_state, config={"callbacks": [handler]})

错误处理：analyze / detect_and_translate 的 LLM 调用失败统一抛
``LLMInvocationError``，原样穿透 ``ainvoke``（LangGraph 不包装节点异常，异常链
保留在 ``__cause__``），由调用方 ``except AnalysisGraphError`` 捕获；两节点各挂
节点级 RetryPolicy 自动重试一次。草稿节点刻意不挂 RetryPolicy、内部捕获全部
异常降级为 ``draft_skipped_reason``（检索失败记一条 warning，其余异常的 LLM
事件已由 handler 记录）——草稿是附加产物，任何草稿失败都不应拖垮已完成的
意向分析。附件图片识别失败在提取函数内部降级（返回 None），不触发节点重试。

日志全部由 GraphTraceHandler 回调实时输出（节点自身零日志，草稿节点的检索
降级 warning 除外），业务上下文（email_id / account_id）由调用方经
``structlog.contextvars`` 注入。

依赖全部闭包注入，无模块级全局，除草稿节点的检索外不触碰 DB。
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Optional, TypedDict

import structlog
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy
from pydantic import ValidationError

from app.agent.constants import (
    ANALYSIS_MAX_BODY_CHARS,
    DRAFT_CHUNK_SNIPPET_CHARS,
    DRAFT_COMPLIANCE_MAX_CHARS,
    DRAFT_MAX_COSINE_DISTANCE,
    DRAFT_QUERY_MAX_CHARS,
    DRAFT_RETRIEVAL_TOP_K,
    HAN_RANGE,
    HANGUL_RANGE,
    KANA_RANGE,
    LLM_CALL_TIMEOUT_SECONDS,
    LLM_NODE_MAX_ATTEMPTS,
    MAX_IMAGES_PER_EMAIL,
    SHORT_SHELL_CHARS,
)
from app.agent.errors import LLMInvocationError
from app.agent.prompts import (
    DRAFT_AFTERSALE_SYSTEM_PROMPT,
    DRAFT_PRESALE_SYSTEM_PROMPT,
    EMAIL_ANALYSIS_SYSTEM_PROMPT,
    EMAIL_TRANSLATE_SYSTEM_PROMPT,
)
from app.schemas.analysis import (
    EVIDENCE_SOURCE_LITERAL,
    INTENT_LITERAL,
    PRIORITY_LITERAL,
    SENTIMENT_LITERAL,
    TRANSLATION_EXCLUDED_INTENTS,
    EmailAnalysisOutput,
    EmailTranslationOutput,
)
from app.schemas.draft import (
    DRAFT_CATEGORY_AFTER_SALE,
    DRAFT_CATEGORY_BY_INTENT,
    DRAFT_CATEGORY_LITERAL,
    DRAFT_CATEGORY_PRE_SALE,
    DRAFT_SKIP_EMPTY_QUERY,
    DRAFT_SKIP_GENERATION_FAILED,
    DRAFT_SKIP_INTENT_MISMATCH,
    DRAFT_SKIP_NO_KNOWLEDGE,
    DRAFT_SKIP_RETRIEVAL_FAILED,
    DRAFT_SKIPPED_REASON_LITERAL,
    EmailDraftOutput,
)
from app.schemas.knowledge import KB_TYPE_FAQ, KB_TYPE_SOP
from app.services.attachment_extract import (
    MAX_EXTRACT_CHARS,
    extract_eml_text,
    extract_image_text,
)
from app.services.preprocess import compose_email_view

#: 检索降级是节点内唯一的非 LLM 日志（LLM 事件全部由 GraphTraceHandler 输出）
_logger = structlog.get_logger(__name__)

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

    # coordinator 放入的附件数据（字节由调用方备好，含 COS 拉取或已有提取缓存）
    attachments: list[dict]

    # coordinator 全量读出的合规红线块文本（不走向量召回），草稿 prompt 注入用
    compliance_rules: list[str]

    # analyze 节点附件提取产出
    attachment_views: list[dict]  # 分层视图段 [{"kind", "filename", "text"}]
    extracted_cache: list[dict]  # [{"attachment_id", "extracted_text"}]，coordinator 写回缓存
    intent_evidence_source: EVIDENCE_SOURCE_LITERAL

    # detect_and_translate 节点产出（译文仅落库展示，不回灌 analyze）
    source_language: str
    translated_subject: str
    translated_text: str

    # analyze 节点产出（枚举字段白名单单一来源在 app/schemas/）
    primary_intent: INTENT_LITERAL
    intents: list[dict]
    reasoning_summary: str
    entities: dict
    sentiment: SENTIMENT_LITERAL
    priority: PRIORITY_LITERAL
    suggested_tools: list[str]
    llm_model: str

    # 草稿节点产出（draft_presale / draft_aftersale，coordinator 落 email_drafts）
    draft_category: DRAFT_CATEGORY_LITERAL
    draft_subject: str
    draft_body: str
    draft_sources: list[dict]  # [{"document_id", "title", "distance", "snippet"}]，人工核对依据
    draft_model: str
    # 草稿降级原因（白名单见 DRAFT_SKIPPED_REASON_LITERAL）
    draft_skipped_reason: DRAFT_SKIPPED_REASON_LITERAL

    # 检索观测证据：实际执行的 query 与原始命中（质量门槛判断前即可观测）。
    # 仅入 state 供调用方日志排查"为什么没出草稿"，不落库——落库的核对依据
    # 仍是 draft_sources；检索前早退（意图类别不匹配 / 空 query）两键缺席
    retrieval_query: str
    retrieved_chunks: list[dict]  # [{"document_id", "title", "distance", "content"}]


async def _extract_attachment_views(
    state: EmailAnalysisState,
    vision_model: Any | None,
) -> tuple[list[dict], list[dict]]:
    """从 state 附件数据提取内容，产出分层视图段与待写回的提取缓存。

    .eml 附件一律递归解析（纯函数，零 LLM 成本）；图片仅当壳层正文极短时
    才做视觉识别（成本控制，每封限量）。已有提取缓存（extracted_text）直接
    复用，不重复拉取与调用；视觉识别失败在提取函数内部降级为 None。
    """
    cleaned = state.get("cleaned_text", "")
    shell_short = len(cleaned.strip()) < SHORT_SHELL_CHARS

    attachment_views: list[dict] = []
    extracted_cache: list[dict] = []
    image_budget = MAX_IMAGES_PER_EMAIL
    for att in state.get("attachments", []):
        kind = att.get("kind")
        text = att.get("extracted_text")
        if not text and att.get("content"):
            if kind == "email":
                text = extract_eml_text(att["content"])
            elif kind == "image" and shell_short and image_budget > 0:
                image_budget -= 1
                text = await extract_image_text(
                    att["content"], att.get("content_type") or "image/png", vision_model
                )
        if not text:
            continue
        text = str(text)[:MAX_EXTRACT_CHARS]
        attachment_views.append({"kind": kind, "filename": att.get("filename"), "text": text})
        if att.get("attachment_id") is not None:
            extracted_cache.append({"attachment_id": att["attachment_id"], "extracted_text": text})
    return attachment_views, extracted_cache


async def _analyze_node(
    state: EmailAnalysisState,
    # langgraph 按注解字面匹配来注入 config，仅认 RunnableConfig/Optional[RunnableConfig]，
    # 不识别 PEP 604 的 `X | None`（本文件启用 future annotations 后注解为字符串，须用 Optional）
    config: Optional[RunnableConfig] = None,  # noqa: UP045
    *,
    chat_model: Any,
    vision_model: Any | None = None,
    max_body_chars: int = ANALYSIS_MAX_BODY_CHARS,
) -> dict:
    """调用 LLM 执行结构化意向分析（含附件内容提取与分层视图组装）。

    附件提取在本节点内完成（不新增节点）：.eml 附件递归解析为文本段，
    图片按需视觉识别，随后与正文拼成带来源标注的分层视图喂给 LLM，
    并输出 ``intent_evidence_source`` 标注意图证据来源。

    langgraph 会把运行时 config 注入到名为 ``config`` 的参数中；
    节点内必须把 config 继续传给内层 runnable，LLM 调用才会进入调用链追踪。

    LLM 调用失败（超时 / 网络 / 解析失败等）统一抛 ``LLMInvocationError``，
    由节点级 RetryPolicy 重试一次、重试耗尽后穿透 ``ainvoke``；图片视觉
    识别失败在提取函数内部降级（返回 None），不触发节点重试。节点自身
    零日志：开始 / 耗时 / usage / 错误均由 GraphTraceHandler 回调输出。
    """
    attachment_views, extracted_cache = await _extract_attachment_views(state, vision_model)

    cleaned = state.get("cleaned_text", "")
    view = compose_email_view(
        subject=state.get("subject", ""),
        sender=state.get("sender"),
        sent_at=state.get("sent_at"),
        cleaned_text=cleaned[:max_body_chars],
        attachment_views=attachment_views,
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
        "intent_evidence_source": result.intent_evidence_source,
        "attachment_views": attachment_views,
        "extracted_cache": extracted_cache,
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

    if not subject.strip() and not cleaned.strip() and not state.get("attachment_views"):
        return {"source_language": "unknown"}
    # 判定输入须包含附件提取文本：防"中文壳层 + 英文 .eml 附件"被误判 zh 跳过翻译
    attachment_text = "".join(
        str(view.get("text") or "") for view in state.get("attachment_views", [])
    )
    if _is_chinese_dominant_text(f"{subject}\n{cleaned}\n{attachment_text}"):
        return {"source_language": "zh"}

    view = compose_email_view(
        subject=subject,
        sender=state.get("sender"),
        sent_at=state.get("sent_at"),
        cleaned_text=cleaned[:max_body_chars],
        attachment_views=state.get("attachment_views"),
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


def _route_after_translation(state: EmailAnalysisState, *, has_retriever: bool) -> str:
    """翻译后的第二次条件路由：命中草稿意图映射且检索器可用时进入对应草稿节点。

    意图不在 ``DRAFT_CATEGORY_BY_INTENT``（ToB / meeting / other / spam / unknown）
    或构建图时未注入 KnowledgeRetriever（embedding 未配置）→ 直接结束，不出草稿。
    """
    if not has_retriever:
        return END
    category = DRAFT_CATEGORY_BY_INTENT.get(state.get("primary_intent") or "")
    if category == DRAFT_CATEGORY_PRE_SALE:
        return "draft_presale"
    if category == DRAFT_CATEGORY_AFTER_SALE:
        return "draft_aftersale"
    return END


def _retrieval_evidence(chunks: list[Any]) -> list[dict]:
    """把检索命中压缩为 state 观测证据（content 截断，distance 保留 4 位）。"""
    return [
        {
            "document_id": chunk.document_id,
            "title": chunk.document_title,
            "distance": round(chunk.distance, 4),
            "content": chunk.content[:DRAFT_CHUNK_SNIPPET_CHARS],
        }
        for chunk in chunks
    ]


async def _draft_node(
    state: EmailAnalysisState,
    # langgraph 按注解字面匹配来注入 config，仅认 RunnableConfig/Optional[RunnableConfig]，
    # 不识别 PEP 604 的 `X | None`（本文件启用 future annotations 后注解为字符串，须用 Optional）
    config: Optional[RunnableConfig] = None,  # noqa: UP045
    *,
    chat_model: Any,
    retriever: Any,
    category: str,
    system_prompt: str,
    kb_type: str,
    max_body_chars: int = ANALYSIS_MAX_BODY_CHARS,
) -> dict:
    """检索知识库并起草待人工确认的回复草稿，失败一律降级、不抛错。

    流程：意图→类别二次校验（防路由错配）→ 组检索 query → 知识库向量检索 →
    质量门槛（无命中或最近余弦距离超 ``DRAFT_MAX_COSINE_DISTANCE`` 视为无相关
    知识，业界共识是低置信不硬答）→ 红线规则（coordinator 全量注入，非召回）
    + 知识摘录 + 分层视图 + 客户语言拼 prompt → 单次 LLM 结构化调用产出
    subject/body。

    检索发生即写观测证据：检索成功后的所有返回路径（含无相关知识、生成失败、
    成功）都带 ``retrieval_query`` 与 ``retrieved_chunks``，调用方可据此在日志
    侧定位"为什么没出草稿"；检索前早退路径两键缺席。

    草稿是意向分析的附加产物：本节点不挂 RetryPolicy、内部捕获全部异常降级为
    ``draft_skipped_reason``，保证任何草稿失败都不影响已完成的意向分析落库。
    检索失败（非 LLM 的网络 IO，handler 覆盖不到）由节点记一条 warning；
    LLM 事件已由 GraphTraceHandler 记录，节点不再重复。
    """
    primary = state.get("primary_intent") or ""
    if DRAFT_CATEGORY_BY_INTENT.get(primary) != category:
        return {"draft_skipped_reason": DRAFT_SKIP_INTENT_MISMATCH}

    subject = state.get("subject", "")
    cleaned = state.get("cleaned_text", "")
    query = f"{subject}\n{cleaned}".strip()[:DRAFT_QUERY_MAX_CHARS]
    if not query:
        return {"draft_skipped_reason": DRAFT_SKIP_EMPTY_QUERY}

    try:
        chunks = await retriever.retrieve(kb_type, query, top_k=DRAFT_RETRIEVAL_TOP_K)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "draft_retrieval_failed",
            category=category,
            kb_type=kb_type,
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
        return {
            "draft_skipped_reason": DRAFT_SKIP_RETRIEVAL_FAILED,
            "retrieval_query": query,
            "retrieved_chunks": [],
        }

    if not chunks or min(chunk.distance for chunk in chunks) > DRAFT_MAX_COSINE_DISTANCE:
        return {
            "draft_skipped_reason": DRAFT_SKIP_NO_KNOWLEDGE,
            "retrieval_query": query,
            "retrieved_chunks": _retrieval_evidence(chunks),
        }

    knowledge_block = "\n\n".join(
        f"[document_id={chunk.document_id} title={chunk.document_title} "
        f"distance={chunk.distance:.4f}]\n{chunk.content}"
        for chunk in chunks
    )
    view = compose_email_view(
        subject=subject,
        sender=state.get("sender"),
        sent_at=state.get("sent_at"),
        cleaned_text=cleaned[:max_body_chars],
        attachment_views=state.get("attachment_views"),
    )

    # 红线规则由 coordinator 全量注入（不走向量召回，保证恒在场）；以整条规则为
    # 单位做字符预算，放不下整条时舍弃其后全部，避免截断出残缺规则误导模型
    sections: list[str] = []
    rule_lines: list[str] = []
    used = 0
    for rule in state.get("compliance_rules", []):
        text = str(rule).strip()
        if not text:
            continue
        line = f"- {text}"
        if used + len(line) + 1 > DRAFT_COMPLIANCE_MAX_CHARS:
            break
        rule_lines.append(line)
        used += len(line) + 1
    if rule_lines:
        sections.append("## 红线规则（知识库，优先级最高）\n\n" + "\n".join(rule_lines))
    sections.append(f"## 知识库摘录\n\n{knowledge_block}")

    human = (
        f"{view}\n\n"
        + "\n\n".join(sections)
        + f"\n\n客户语言: {state.get('source_language') or 'unknown'}"
    )

    try:
        structured = chat_model.with_structured_output(EmailDraftOutput, method="function_calling")
        result = await asyncio.wait_for(
            structured.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=human),
                ],
                config=config,
            ),
            timeout=LLM_CALL_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001
        # LLM 超时/网络/解析失败均降级；具体错误已由 GraphTraceHandler 记录
        return {
            "draft_skipped_reason": DRAFT_SKIP_GENERATION_FAILED,
            "retrieval_query": query,
            "retrieved_chunks": _retrieval_evidence(chunks),
        }

    if result is None:
        return {
            "draft_skipped_reason": DRAFT_SKIP_GENERATION_FAILED,
            "retrieval_query": query,
            "retrieved_chunks": _retrieval_evidence(chunks),
        }

    return {
        "draft_category": category,
        "draft_subject": result.subject,
        "draft_body": result.body,
        "draft_sources": [
            {
                "document_id": chunk.document_id,
                "title": chunk.document_title,
                "distance": round(chunk.distance, 4),
                "snippet": chunk.content[:200],
            }
            for chunk in chunks
        ],
        "draft_model": getattr(chat_model, "model_name", "unknown"),
        "retrieval_query": query,
        "retrieved_chunks": _retrieval_evidence(chunks),
    }


def build_email_analysis_graph(
    chat_model: Any, vision_model: Any | None = None, knowledge_retriever: Any | None = None
):
    """构建单邮件意向分析图，依赖全部闭包注入。

    ``vision_model`` 为视觉 LLM 客户端（识别图片附件用），None 时跳过图片
    识别（.eml 附件解析不受影响）。

    ``knowledge_retriever`` 为知识库检索器（回复草稿用），None 时草稿分支
    永不进入（路由直达 END），图结构不变。

    调用链追踪：ainvoke 时传 ``config={"callbacks": [GraphTraceHandler()]}``，
    结束后由 ``handler.dump()`` 取事件列表（用法见模块 docstring）。

    analyze 与 detect_and_translate 节点各挂 RetryPolicy：仅对 LLMInvocationError
    重试（次数见 ``constants.LLM_NODE_MAX_ATTEMPTS``，含首次），重试耗尽后异常
    原样抛给 ainvoke 调用方。草稿节点不挂 RetryPolicy，内部全量降级（见
    ``_draft_node`` docstring）。

    条件路由 ×2：analyze 之后按主意图排除垃圾/通知类；detect_and_translate
    之后按 ``DRAFT_CATEGORY_BY_INTENT`` 映射进入售前/售后草稿节点。
    """

    builder = StateGraph(EmailAnalysisState)

    builder.add_node(
        "analyze",
        partial(_analyze_node, chat_model=chat_model, vision_model=vision_model),
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
    builder.add_node(
        "draft_presale",
        partial(
            _draft_node,
            chat_model=chat_model,
            retriever=knowledge_retriever,
            category=DRAFT_CATEGORY_PRE_SALE,
            system_prompt=DRAFT_PRESALE_SYSTEM_PROMPT,
            kb_type=KB_TYPE_FAQ,
        ),
    )
    builder.add_node(
        "draft_aftersale",
        partial(
            _draft_node,
            chat_model=chat_model,
            retriever=knowledge_retriever,
            category=DRAFT_CATEGORY_AFTER_SALE,
            system_prompt=DRAFT_AFTERSALE_SYSTEM_PROMPT,
            kb_type=KB_TYPE_SOP,
        ),
    )

    builder.add_conditional_edges(
        "analyze",
        _route_translation_by_primary_intent,
        {"detect_and_translate": "detect_and_translate", END: END},
    )
    builder.add_conditional_edges(
        "detect_and_translate",
        partial(_route_after_translation, has_retriever=knowledge_retriever is not None),
        {
            "draft_presale": "draft_presale",
            "draft_aftersale": "draft_aftersale",
            END: END,
        },
    )
    builder.add_edge("draft_presale", END)
    builder.add_edge("draft_aftersale", END)

    builder.set_entry_point("analyze")

    return builder.compile()
