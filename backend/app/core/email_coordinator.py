"""邮件智能体编排器：集中管理清洗、DB 读写与意向分析图。

唯一的 langgraph 是 ``build_email_analysis_graph``（从清洗后的文本进入 LLM 意向分析，
命中售前/售后意图时在图内续接草稿分支检索知识库起草回复）。本类负责从数据库读取
邮件与附件、清洗正文、装配并驱动该图、草稿落库 email_drafts 待人工确认，以及批量
分析入口；附件字节从 COS 拉取、提取缓存写回均在本类完成（分析图自身不触 IO）。
本系统不发送邮件，草稿由人工确认后在系统之外发送。
"""

from __future__ import annotations

import structlog

from app.agent.analysis_graph import build_email_analysis_graph
from app.agent.errors import AnalysisGraphError
from app.agent.trace_handle import GraphTraceHandler
from app.core.settings import AppConfig
from app.db.db import EmailAnalysis, EmailDraft, EmailMessage
from app.db.engine import Database
from app.db.repositories import (
    EmailAnalysisRepository,
    EmailAttachmentRepository,
    EmailDraftRepository,
    EmailRepository,
    KbChunkRepository,
)
from app.llm import build_chat_model
from app.schemas.analysis import EVIDENCE_BODY, UNKNOWN_INTENT
from app.schemas.knowledge import KB_TYPE_COMPLIANCE
from app.services.preprocess import preprocess_email_text


class EmailCoordinator:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        logger,
        attachment_storage=None,
        knowledge_retriever=None,
    ):
        self._config = config
        self._database = database
        self._logger = logger
        self._storage = attachment_storage
        # 知识库检索器（草稿分支用）；None 时图内草稿分支永不进入
        self._retriever = knowledge_retriever

        self._chat_model = build_chat_model(self._config.llm)
        # 视觉模型（识别图片附件用）；未配置则跳过图片识别
        self._vision_model = (
            build_chat_model(self._config.llm, model=self._config.llm.llm_vision_model)
            if self._config.llm.llm_vision_model
            else None
        )
        self._analysis_graph = build_email_analysis_graph(
            self._chat_model, self._vision_model, self._retriever
        )

    async def _load_attachment_input(self, email_id: int) -> list[dict]:
        """读取附件元数据并按需从 COS 拉取字节，组装分析图的附件输入。

        已有提取缓存（extracted_text）的直接复用，不重复拉取；
        COS 未配置或拉取失败时跳过该附件（分析基于剩余内容继续）。
        """
        async with self._database.session() as session:
            repo = EmailAttachmentRepository(session)
            attachments = await repo.list_email_attachment_by_email_id(email_id)

        attachment_input: list[dict] = []
        for att in attachments:
            if att.kind not in ("email", "image"):
                continue
            item: dict = {
                "attachment_id": att.id,
                "kind": att.kind,
                "filename": att.filename,
                "content_type": att.content_type,
                "content": None,
                "extracted_text": att.extracted_text,
            }
            if item["extracted_text"] is None and att.storage_key and self._storage is not None:
                try:
                    item["content"] = await self._storage.fetch(att.storage_key)
                except Exception as exc:  # noqa: BLE001
                    self._logger.warning(
                        "attachment_fetch_failed",
                        email_id=email_id,
                        attachment_id=att.id,
                        storage_key=att.storage_key,
                        error=str(exc),
                    )
                    continue
            if item["content"] or item["extracted_text"]:
                attachment_input.append(item)
        return attachment_input

    async def _save_extracted_cache(self, cache: list[dict]) -> None:
        """把 analyze 节点的附件提取结果写回 email_attachments.extracted_text 缓存。"""
        if not cache:
            return
        async with self._database.session() as session:
            repo = EmailAttachmentRepository(session)
            for item in cache:
                await repo.update_email_attachment_extracted_text_by_id(
                    item["attachment_id"], item["extracted_text"]
                )

    async def _load_compliance_rules(self) -> list[str]:
        """读取生效中的合规红线块文本，供草稿 prompt 全量注入（不走向量召回）。

        红线规则是草稿的安全底线，必须恒在场，因此不做相似度召回；
        知识库未建表或读取失败时降级为空列表（记 warning）——草稿仅落库供
        人工审阅、prompt 内置硬约束仍生效，任何失败都不拖垮意向分析主链。
        """
        try:
            async with self._database.session() as session:
                chunks = await KbChunkRepository(session).list_kb_chunk_by_kb_type(
                    KB_TYPE_COMPLIANCE, active_only=True
                )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "compliance_rules_load_failed", error=f"{type(exc).__name__}: {exc}"[:300]
            )
            return []
        return [chunk.content for chunk in chunks]

    async def _save_draft(self, email: EmailMessage, result: dict) -> dict | None:
        """把草稿节点产出落 email_drafts（幂等覆盖，status 重置回 pending）。

        图未产出草稿（意图未命中 / 无相关知识 / 检索或生成失败）时记日志并
        返回 None——不出草稿即转人工，符合"低置信不硬答"的处理共识。
        """
        draft_subject = result.get("draft_subject")
        draft_body = result.get("draft_body")
        draft_category = result.get("draft_category")
        if not (draft_subject and draft_body and draft_category):
            reason = result.get("draft_skipped_reason")
            if reason:
                self._logger.info(
                    "draft_skipped", email_id=email.id, category=draft_category, reason=reason
                )
            return None

        async with self._database.session() as session:
            repo = EmailDraftRepository(session)
            entity = EmailDraft(
                email_id=email.id,
                account_id=email.account_id,
                category=draft_category,
                subject=draft_subject,
                body=draft_body,
                sources=result.get("draft_sources", []),
                model=result.get("draft_model"),
            )
            saved = await repo.upsert_email_draft(entity)

        self._logger.info(
            "draft_saved",
            email_id=email.id,
            draft_id=saved.id,
            category=saved.category,
            status=saved.status,
        )
        return {"draft_id": saved.id, "category": saved.category, "status": saved.status}

    async def analyze_email(self, email_id: int) -> dict:

        email: EmailMessage | None
        # 1. 从 DB 读取邮件
        async with self._database.session() as session:
            email = await EmailRepository(session).get_email_by_id(email_id)
        if email is None or email.id is None:
            raise LookupError(f"email not found: {email_id}")

        # 2. 清洗正文
        cleaned_text = preprocess_email_text(
            email.text_body,
            email.html_body,
        )

        # 3. 附件输入：元数据 + COS 拉取的字节 / 已有提取缓存
        attachment_input = await self._load_attachment_input(email.id)

        # 4. 合规红线全量读取（读失败降级为空，不拖垮主链）
        compliance_rules = await self._load_compliance_rules()

        # 5. 构建初始状态并驱动分析图，结束后一次性记录完整调用链
        initial = {
            "email_id": email.id,
            "account_id": email.account_id,
            "subject": email.subject or "",
            "sender": email.sender,
            "sent_at": email.sent_at,
            "cleaned_text": cleaned_text,
            "attachments": attachment_input,
            "compliance_rules": compliance_rules,
        }

        trace = GraphTraceHandler()
        # 业务上下文经 contextvars 注入，全链路日志（含 GraphTraceHandler 回调）自动携带；
        # 异步安全：绑定随 asyncio task 复制，批处理循环内互不污染
        with structlog.contextvars.bound_contextvars(
            email_id=email.id, account_id=email.account_id
        ):
            try:
                result: dict = await self._analysis_graph.ainvoke(
                    initial, config={"callbacks": [trace]}
                )
                error_info: AnalysisGraphError | None = None
            except AnalysisGraphError as exc:
                # 图内可预期失败（LLM 调用类）；逻辑 bug 等意外异常继续向上抛
                self._logger.error(
                    "analysis_graph_failed",
                    email_id=email.id,
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                    exc_info=True,
                )
                result, error_info = {}, exc

            # 6. 提取缓存写回（失败路径无部分状态，跳过即可）
            await self._save_extracted_cache(result.get("extracted_cache") or [])

            # 7. 落库分析结果（成功或失败兜底）
            async with self._database.session() as session:
                repo = EmailAnalysisRepository(session)
                entity = EmailAnalysis(
                    email_id=email.id,
                    account_id=email.account_id,
                    primary_intent=result.get("primary_intent", UNKNOWN_INTENT),
                    intents=result.get("intents", []),
                    reasoning_summary=result.get("reasoning_summary", ""),
                    entities=result.get("entities", {}),
                    sentiment=result.get("sentiment", "neutral"),
                    priority=result.get("priority", "P2"),
                    suggested_tools=result.get("suggested_tools", []),
                    status="analyzed",
                    model=result.get("llm_model"),
                    # 译文类字段无默认值兜底：中文/垃圾邮件/失败路径本就无译文
                    source_language=result.get("source_language"),
                    translated_subject=result.get("translated_subject"),
                    translated_text=result.get("translated_text"),
                    intent_evidence_source=result.get("intent_evidence_source", EVIDENCE_BODY),
                )

                if error_info is not None:
                    entity.priority = "P1"
                    entity.sentiment = "neutral"
                    entity.status = "failed"
                    entity.error = str(error_info)

                saved = await repo.upsert_email_analysis(entity)

            # 8. 草稿落库：仅分析成功路径尝试，失败路径不出草稿
            draft = None
            if error_info is None:
                draft = await self._save_draft(email, result)

            return {
                "email_id": email.id,
                "analysis_id": saved.id,
                "status": "failed" if error_info is not None else "analyzed",
                "primary_intent": result.get("primary_intent", UNKNOWN_INTENT),
                "priority": result.get("priority", "P2"),
                "intent_evidence_source": result.get("intent_evidence_source", EVIDENCE_BODY),
                "draft": draft,
                "error": str(error_info) if error_info is not None else None,
                "error_type": type(error_info).__name__ if error_info is not None else None,
            }

    async def start_analyze(self, *, account_id: int | None = None, limit: int = 20) -> list[dict]:

        async with self._database.session() as session:
            unanalyzed = await EmailRepository(session).list_unanalyzed_email(
                account_id=account_id, limit=limit
            )

        results: list[dict] = []

        for email in unanalyzed:
            if email.id is None:
                continue

            try:
                outcome = await self.analyze_email(email.id)
                results.append(outcome)

                self._logger.info(
                    "email_analyzed",
                    email_id=email.id,
                    intent=outcome.get("primary_intent"),
                    priority=outcome.get("priority"),
                    analysis_id=outcome.get("analysis_id"),
                )

            except Exception as exc:  # noqa: BLE001
                self._logger.error(
                    "email_analysis_failed",
                    email_id=email.id,
                    error=f"{type(exc).__name__}: {exc}",
                )

        return results
