"""邮件智能体编排器：集中管理清洗、DB 读写与意向分析图。

唯一的 langgraph 是 ``build_email_analysis_graph``（从清洗后的文本进入 LLM 意向分析）。
本类负责从数据库读取邮件与附件、清洗正文、装配并驱动该图，以及批量分析入口；
附件字节从 COS 拉取、提取缓存写回均在本类完成（分析图自身不触 IO）。
"""

from __future__ import annotations

import structlog

from app.agent.analysis_graph import build_email_analysis_graph
from app.agent.errors import AnalysisGraphError
from app.agent.trace_handle import GraphTraceHandler
from app.core.settings import AppConfig
from app.db.db import EmailAnalysis, EmailMessage
from app.db.engine import Database
from app.db.repositories import EmailAnalysisRepository, EmailAttachmentRepository, EmailRepository
from app.llm import build_chat_model
from app.schemas.analysis import EVIDENCE_BODY, UNKNOWN_INTENT
from app.services.preprocess import preprocess_email_text


class EmailCoordinator:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        logger,
        attachment_storage=None,
    ):
        self._config = config
        self._database = database
        self._logger = logger
        self._storage = attachment_storage

        self._chat_model = build_chat_model(self._config.llm)
        # 视觉模型（识别图片附件用）；未配置则跳过图片识别
        self._vision_model = (
            build_chat_model(self._config.llm, model=self._config.llm.llm_vision_model)
            if self._config.llm.llm_vision_model
            else None
        )
        self._analysis_graph = build_email_analysis_graph(self._chat_model, self._vision_model)

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

        # 4. 构建初始状态并驱动分析图，结束后一次性记录完整调用链
        initial = {
            "email_id": email.id,
            "account_id": email.account_id,
            "subject": email.subject or "",
            "sender": email.sender,
            "sent_at": email.sent_at,
            "cleaned_text": cleaned_text,
            "attachments": attachment_input,
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

            # 5. 提取缓存写回（失败路径无部分状态，跳过即可）
            await self._save_extracted_cache(result.get("extracted_cache") or [])

            # 6. 落库分析结果（成功或失败兜底）
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

            return {
                "email_id": email.id,
                "analysis_id": saved.id,
                "status": "failed" if error_info is not None else "analyzed",
                "primary_intent": result.get("primary_intent", UNKNOWN_INTENT),
                "priority": result.get("priority", "P2"),
                "intent_evidence_source": result.get("intent_evidence_source", EVIDENCE_BODY),
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
