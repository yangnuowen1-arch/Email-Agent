"""邮件智能体编排器：集中管理清洗、DB 读写与意向分析图。

唯一的 langgraph 是 ``build_email_analysis_graph``（从清洗后的文本进入 LLM 意向分析）。
本类负责从数据库读取邮件、清洗正文、装配并驱动该图，以及批量分析入口。
"""

from __future__ import annotations

from app.agent.analysis_graph import GraphTraceHandler, build_email_analysis_graph
from app.core.settings import AppConfig
from app.db.db import EmailAnalysis, EmailMessage
from app.db.engine import Database
from app.db.repositories import EmailAnalysisRepository, EmailRepository
from app.llm import build_chat_model
from app.schemas.analysis import UNKNOWN_INTENT
from app.services.preprocess import preprocess_email_text


class EmailCoordinator:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        logger,
    ):
        self._config = config
        self._database = database
        self._logger = logger

        self._chat_model = build_chat_model(self._config.llm)
        self._analysis_graph = build_email_analysis_graph(self._chat_model)

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

        # 3. 构建初始状态并驱动分析图，结束后一次性记录完整调用链
        initial = {
            "email_id": email.id,
            "account_id": email.account_id,
            "subject": email.subject or "",
            "sender": email.sender,
            "sent_at": email.sent_at,
            "cleaned_text": cleaned_text,
        }

        trace = GraphTraceHandler()
        result = await self._analysis_graph.ainvoke(initial, config={"callbacks": [trace]})
        self._logger.info("graph_trace", email_id=email.id, events=trace.dump())

        # 4. 落库分析结果（成功或失败兜底）
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
            )

            if result.get("error"):
                entity.priority = "P1"
                entity.sentiment = "neutral"
                entity.status = "failed"
                entity.error = result["error"]

            saved = await repo.upsert_email_analysis(entity)

        return {
            "email_id": email.id,
            "analysis_id": saved.id,
            "status": result.get("error") and "failed" or "analyzed",
            "primary_intent": result.get("primary_intent", UNKNOWN_INTENT),
            "priority": result.get("priority", "P2"),
            "error": result.get("error"),
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
