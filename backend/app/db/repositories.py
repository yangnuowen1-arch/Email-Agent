"""异步数据访问层：一个表一个 Repository。

所有 Repository 的会话由构造函数注入，事务边界由 ``Database.session()`` 管理。
Repository 内部不提交事务，仅通过 ``flush()`` 获取主键。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import ParsedEmail
from app.schemas.draft import ALL_DRAFT_STATUSES, DRAFT_STATUS_PENDING
from app.schemas.knowledge import ALL_KB_TYPES, KB_EMBEDDING_DIMENSIONS, KB_STATUS_ACTIVE

from .db import (
    Account,
    EmailAnalysis,
    EmailAttachment,
    EmailDraft,
    EmailMessage,
    KbChunk,
    KbDocument,
)


class EmailAccountRepository:
    """email_accounts 表的异步访问入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_account_by_id(self, account_id: int) -> Account | None:
        """按主键查询单个账号。"""
        return await self.session.get(Account, account_id)

    async def list_account(self, *, enabled_only: bool = False) -> list[Account]:
        """查询账号列表，默认返回全部；enabled_only=True 时仅返回启用的账号。"""
        stmt = select(Account).order_by(Account.id)
        if enabled_only:
            stmt = stmt.where(Account.enabled.is_(True))

        result = await self.session.scalars(stmt)
        return list(result.all())

    async def create_account(self, account: Account) -> Account:
        """插入单个账号，flush 后返回带主键的实体。"""
        if not isinstance(account, Account):
            msg = f"expected Account, got {type(account).__name__}"
            raise TypeError(msg)

        self.session.add(account)
        await self.session.flush()
        return account

    async def update_account_checkpoint(
        self,
        account_id: int,
        last_sync_uid: int,
        last_sync_at: datetime | None = None,
    ) -> None:
        """推进账号的增量断点（last_sync_uid / last_sync_at）。"""
        if not isinstance(account_id, int) or account_id <= 0:
            msg = f"account_id must be positive int, got {account_id!r}"
            raise ValueError(msg)
        if not isinstance(last_sync_uid, int) or last_sync_uid < 0:
            msg = f"last_sync_uid must be int >=0, got {last_sync_uid!r}"
            raise ValueError(msg)
        if last_sync_at is None:
            last_sync_at = datetime.now(UTC)

        stmt = (
            update(Account)
            .where(Account.id == account_id)
            .values(last_sync_uid=last_sync_uid, last_sync_at=last_sync_at)
        )
        await self.session.execute(stmt)

    async def delete_account_by_id(self, account_id: int) -> bool:
        """按主键删除账号，返回是否删除成功。"""
        stmt = delete(Account).where(Account.id == account_id)
        result = await self.session.execute(stmt)
        return bool(result.rowcount)


class EmailRepository:
    """emails 表的异步访问入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_email_by_id(self, email_id: int) -> EmailMessage | None:
        """按主键查询单封邮件。"""
        return await self.session.get(EmailMessage, email_id)

    async def get_email(self, account_id: int, uid: int) -> EmailMessage | None:
        """按 (account_id, uid) 复合键查询单封邮件。"""
        stmt = select(EmailMessage).where(
            EmailMessage.account_id == account_id,
            EmailMessage.uid == uid,
        )
        return await self.session.scalar(stmt)

    async def list_email(self) -> list[EmailMessage]:
        """查询所有邮件。"""
        stmt = select(EmailMessage).order_by(EmailMessage.id)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_email_by_account_id(self, account_id: int) -> list[EmailMessage]:
        """按账号查询邮件列表。"""
        stmt = (
            select(EmailMessage)
            .where(
                EmailMessage.account_id == account_id,
            )
            .order_by(EmailMessage.id)
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_unread_email(
        self, *, account_id: int | None = None, limit: int = 20
    ) -> list[ParsedEmail]:
        """查询未读邮件，可选按账号过滤；按 sent_at 倒序 + limit。

        LEFT JOIN email_analyses，将分析结果投影进 ParsedEmail.analysis。
        返回领域类型 ``ParsedEmail``，隔离 ORM 出上层。
        """
        if limit <= 0:
            msg = f"limit must be positive int, got {limit!r}"
            raise ValueError(msg)

        ea = EmailAnalysis.__table__
        stmt = (
            select(EmailMessage, EmailAnalysis)
            .outerjoin(ea, EmailAnalysis.email_id == EmailMessage.id)
            .where(EmailMessage.is_read.is_(False))
        )

        if account_id is not None:
            stmt = stmt.where(EmailMessage.account_id == account_id)

        stmt = stmt.order_by(EmailMessage.sent_at.desc()).limit(limit)

        rows = await self.session.execute(stmt)
        return [self._to_parsed_email(email, analysis) for email, analysis in rows]

    async def list_unanalyzed_email(
        self, *, account_id: int | None = None, limit: int = 20
    ) -> list[ParsedEmail]:
        """查询未分析的未读邮件：LEFT JOIN 排除已有分析行。

        语义：存在分析行（含 failed）即不再自动重跑。
        """
        if limit <= 0:
            msg = f"limit must be positive int, got {limit!r}"
            raise ValueError(msg)

        ea = EmailAnalysis.__table__
        stmt = (
            select(EmailMessage)
            .outerjoin(ea, EmailAnalysis.email_id == EmailMessage.id)
            .where(EmailMessage.is_read.is_(False))
            .where(EmailAnalysis.id.is_(None))
        )

        if account_id is not None:
            stmt = stmt.where(EmailMessage.account_id == account_id)

        stmt = stmt.order_by(EmailMessage.sent_at.desc()).limit(limit)

        rows = await self.session.scalars(stmt)
        return [self._to_parsed_email(row) for row in rows]

    async def mark_emails_read(self, ids: list[int]) -> int:
        """按主键批量标记已读，返回实际影响行数。"""
        if not ids:
            return 0

        stmt = update(EmailMessage).where(EmailMessage.id.in_(ids)).values(is_read=True)
        result = await self.session.execute(stmt)
        return result.rowcount

    async def list_recent_emails(
        self, *, account_id: int | None = None, limit: int = 5
    ) -> list[ParsedEmail]:
        """按 sent_at 倒序返回最近 limit 封邮件；account_id 为 None 时跨全部账号。

        返回领域类型 ``ParsedEmail``，隔离 ORM 出上层。
        """
        if limit <= 0:
            msg = f"limit must be positive int, got {limit!r}"
            raise ValueError(msg)

        stmt = select(EmailMessage)
        if account_id is not None:
            stmt = stmt.where(EmailMessage.account_id == account_id)
        stmt = stmt.order_by(EmailMessage.sent_at.desc()).limit(limit)

        rows = await self.session.scalars(stmt)
        return [self._to_parsed_email(row) for row in rows]

    @staticmethod
    def _to_parsed_email(row: EmailMessage, analysis: EmailAnalysis | None = None) -> ParsedEmail:
        """ORM 行 -> 领域类型 ``ParsedEmail`` 的投影。"""
        analysis_dict: dict | None = None
        if analysis is not None:
            analysis_dict = {
                "primary_intent": analysis.primary_intent,
                "intents": analysis.intents,
                "reasoning_summary": analysis.reasoning_summary,
                "entities": analysis.entities,
                "sentiment": analysis.sentiment,
                "priority": analysis.priority,
                "suggested_tools": analysis.suggested_tools,
                "status": analysis.status,
                "error": analysis.error,
            }
        return ParsedEmail(
            account_id=row.account_id,
            uid=row.uid,
            id=row.id,
            message_id=row.message_id,
            subject=row.subject or "",
            sender=row.sender,
            recipients=row.recipients or [],
            sent_at=row.sent_at,
            text_body=row.text_body,
            html_body=row.html_body,
            fetched_at=row.fetched_at,
            analysis=analysis_dict,
        )

    async def create_email(self, message: EmailMessage) -> EmailMessage:
        """插入单封邮件，flush 后返回带主键的实体。"""
        if not isinstance(message, EmailMessage):
            msg = f"expected EmailMessage, got {type(message).__name__}"
            raise TypeError(msg)

        self.session.add(message)
        await self.session.flush()
        return message

    async def bulk_create_email(self, messages: list[EmailMessage]) -> dict[tuple[int, int], int]:
        """批量插入邮件，利用 (account_id, uid) 幂等去重。

        返回 {(account_id, uid): 新插入的 email_id} 映射；冲突未插入的邮件
        不在映射中，调用方据此跳过其关联数据（如附件），保证幂等。
        """
        if not messages:
            return {}

        values: list[dict] = []
        for m in messages:
            if not isinstance(m, EmailMessage):
                msg = f"expected EmailMessage, got {type(m).__name__}"
                raise TypeError(msg)

            fetched_at = m.fetched_at or datetime.now(UTC)
            values.append(
                {
                    "account_id": m.account_id,
                    "uid": m.uid,
                    "message_id": m.message_id,
                    "subject": m.subject,
                    "sender": m.sender,
                    "recipients": m.recipients,
                    "sent_at": m.sent_at,
                    "text_body": m.text_body,
                    "html_body": m.html_body,
                    "fetched_at": fetched_at,
                }
            )

        stmt = (
            pg_insert(EmailMessage)
            .values(values)
            .on_conflict_do_nothing(index_elements=["account_id", "uid"])
            .returning(EmailMessage.account_id, EmailMessage.uid, EmailMessage.id)
        )
        result = await self.session.execute(stmt)
        return {(row.account_id, row.uid): row.id for row in result.fetchall()}

    async def delete_email_by_id(self, email_id: int) -> bool:
        """按主键删除邮件，返回是否删除成功。"""
        stmt = delete(EmailMessage).where(EmailMessage.id == email_id)
        result = await self.session.execute(stmt)
        return bool(result.rowcount)


# ---------------------------------------------------------------------------
# 邮件附件 Repository
# ---------------------------------------------------------------------------


class EmailAttachmentRepository:
    """email_attachments 表的异步访问入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_create_email_attachment(self, entities: list[EmailAttachment]) -> int:
        """批量插入附件元数据，返回插入条数（附件引用由调用方保证幂等）。"""
        if not entities:
            return 0

        values: list[dict] = []
        for e in entities:
            if not isinstance(e, EmailAttachment):
                msg = f"expected EmailAttachment, got {type(e).__name__}"
                raise TypeError(msg)
            values.append(
                {
                    "email_id": e.email_id,
                    "kind": e.kind,
                    "filename": e.filename,
                    "content_type": e.content_type,
                    "disposition": e.disposition,
                    "content_id": e.content_id,
                    "size": e.size,
                    "storage_url": e.storage_url,
                    "storage_key": e.storage_key,
                }
            )

        await self.session.execute(pg_insert(EmailAttachment).values(values))
        return len(values)

    async def list_email_attachment_by_email_id(self, email_id: int) -> list[EmailAttachment]:
        """按所属邮件查询附件列表，按插入顺序返回。"""
        stmt = (
            select(EmailAttachment)
            .where(EmailAttachment.email_id == email_id)
            .order_by(EmailAttachment.id)
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def update_email_attachment_extracted_text_by_id(
        self, attachment_id: int, extracted_text: str
    ) -> bool:
        """写回附件内容提取缓存（.eml 解析文本 / 图片识别文本）。"""
        stmt = (
            update(EmailAttachment)
            .where(EmailAttachment.id == attachment_id)
            .values(extracted_text=extracted_text, extracted_at=datetime.now(UTC))
        )
        result = await self.session.execute(stmt)
        return bool(result.rowcount)


# ---------------------------------------------------------------------------
# 邮件结构化分析 Repository
# ---------------------------------------------------------------------------


class EmailAnalysisRepository:
    """email_analyses 表的异步访问入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_email_analysis_by_email_id(self, email_id: int) -> EmailAnalysis | None:
        """按唯一键查询单条分析结果。"""
        stmt = select(EmailAnalysis).where(EmailAnalysis.email_id == email_id)
        return await self.session.scalar(stmt)

    async def create_email_analysis(self, entity: EmailAnalysis) -> EmailAnalysis:
        """插入单条分析结果，flush 后返回带主键的实体。"""
        if not isinstance(entity, EmailAnalysis):
            msg = f"expected EmailAnalysis, got {type(entity).__name__}"
            raise TypeError(msg)

        self.session.add(entity)
        await self.session.flush()
        return entity

    async def upsert_email_analysis(self, entity: EmailAnalysis) -> EmailAnalysis:
        """幂等写入：ON CONFLICT (email_id) DO UPDATE，返回带 id 的实体。

        对应 db-schema.md §2.3 的写入方式。
        """
        if not isinstance(entity, EmailAnalysis):
            msg = f"expected EmailAnalysis, got {type(entity).__name__}"
            raise TypeError(msg)

        now = datetime.now(UTC)
        stmt = (
            pg_insert(EmailAnalysis)
            .values(
                email_id=entity.email_id,
                account_id=entity.account_id,
                primary_intent=entity.primary_intent,
                intents=entity.intents,
                reasoning_summary=entity.reasoning_summary,
                entities=entity.entities,
                sentiment=entity.sentiment,
                priority=entity.priority,
                suggested_tools=entity.suggested_tools,
                status=entity.status,
                error=entity.error,
                model=entity.model,
                source_language=entity.source_language,
                translated_subject=entity.translated_subject,
                translated_text=entity.translated_text,
                intent_evidence_source=entity.intent_evidence_source,
            )
            .on_conflict_do_update(
                index_elements=["email_id"],
                set_={
                    "account_id": entity.account_id,
                    "primary_intent": entity.primary_intent,
                    "intents": entity.intents,
                    "reasoning_summary": entity.reasoning_summary,
                    "entities": entity.entities,
                    "sentiment": entity.sentiment,
                    "priority": entity.priority,
                    "suggested_tools": entity.suggested_tools,
                    "status": entity.status,
                    "error": entity.error,
                    "model": entity.model,
                    "source_language": entity.source_language,
                    "translated_subject": entity.translated_subject,
                    "translated_text": entity.translated_text,
                    "intent_evidence_source": entity.intent_evidence_source,
                    "updated_at": now,
                },
            )
            .returning(EmailAnalysis.id)
        )
        result = await self.session.execute(stmt)
        row = result.fetchone()
        if row:
            entity.id = row[0]  # type: ignore[assignment]
        return entity

    async def update_email_analysis_status_by_id(
        self, analysis_id: int, status: str, error: str | None = None
    ) -> None:
        """更新分析状态（failed 场景）。"""
        values: dict = {"status": status, "updated_at": datetime.now(UTC)}
        if error is not None:
            values["error"] = error

        stmt = update(EmailAnalysis).where(EmailAnalysis.id == analysis_id).values(**values)
        await self.session.execute(stmt)


# ---------------------------------------------------------------------------
# 回复草稿 Repository（人工确认流：状态流转见 docs/db-schema.md §2.9）
# ---------------------------------------------------------------------------


class EmailDraftRepository:
    """email_drafts 表的异步访问入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_email_draft_by_id(self, draft_id: int) -> EmailDraft | None:
        """按主键查询单条草稿。"""
        return await self.session.get(EmailDraft, draft_id)

    async def get_email_draft_by_email_id(self, email_id: int) -> EmailDraft | None:
        """按唯一键查询单条草稿。"""
        stmt = select(EmailDraft).where(EmailDraft.email_id == email_id)
        return await self.session.scalar(stmt)

    async def list_email_draft_by_status(self, status: str) -> list[EmailDraft]:
        """按确认状态查询草稿列表（人工确认队列），按生成时间倒序。"""
        stmt = (
            select(EmailDraft)
            .where(EmailDraft.status == status)
            .order_by(EmailDraft.created_at.desc())
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def upsert_email_draft(self, entity: EmailDraft) -> EmailDraft:
        """幂等写入：ON CONFLICT (email_id) DO UPDATE，返回带 id 的实体。

        重生成即整体覆盖最新草稿，status 重置回 pending 等待重新确认。
        对应 db-schema.md §2.9 的写入方式。
        """
        if not isinstance(entity, EmailDraft):
            msg = f"expected EmailDraft, got {type(entity).__name__}"
            raise TypeError(msg)

        now = datetime.now(UTC)
        stmt = (
            pg_insert(EmailDraft)
            .values(
                email_id=entity.email_id,
                account_id=entity.account_id,
                category=entity.category,
                status=DRAFT_STATUS_PENDING,
                subject=entity.subject,
                body=entity.body,
                sources=entity.sources,
                model=entity.model,
            )
            .on_conflict_do_update(
                index_elements=["email_id"],
                set_={
                    "account_id": entity.account_id,
                    "category": entity.category,
                    "status": DRAFT_STATUS_PENDING,
                    "subject": entity.subject,
                    "body": entity.body,
                    "sources": entity.sources,
                    "model": entity.model,
                    "updated_at": now,
                },
            )
            .returning(EmailDraft.id)
        )
        result = await self.session.execute(stmt)
        row = result.fetchone()
        if row:
            entity.id = row[0]  # type: ignore[assignment]
        return entity

    async def update_email_draft_status_by_id(self, draft_id: int, status: str) -> bool:
        """人工确认动作：更新草稿确认状态，返回是否存在该草稿。"""
        if status not in set(ALL_DRAFT_STATUSES):
            msg = f"status must be one of {sorted(ALL_DRAFT_STATUSES)}, got {status!r}"
            raise ValueError(msg)

        stmt = (
            update(EmailDraft)
            .where(EmailDraft.id == draft_id)
            .values(status=status, updated_at=datetime.now(UTC))
        )
        result = await self.session.execute(stmt)
        return bool(result.rowcount)


# ---------------------------------------------------------------------------
# 知识库 Repository（RAG，与邮件链路零接线）
# ---------------------------------------------------------------------------


def _validate_kb_type(kb_type: str) -> None:
    """kb_type 白名单校验：非法值在仓储入口即失败，不落库。"""
    if kb_type not in ALL_KB_TYPES:
        msg = f"kb_type must be one of {sorted(ALL_KB_TYPES)}, got {kb_type!r}"
        raise ValueError(msg)


class KbChunkSimilarityRow(NamedTuple):
    """相似度检索行：分块 + 所属文档标题 + 余弦距离（距离越小越相似）。"""

    chunk: KbChunk
    document_title: str
    distance: float


def _build_similarity_stmt(
    kb_type: str,
    query_embedding: list[float],
    *,
    top_k: int,
    embedding_model: str | None = None,
):
    """构造余弦相似度检索语句（纯函数，便于单测检验过滤与排序封装）。

    过滤：kb_type 精确匹配 + join 所属文档要求 status=active（归档知识不参与
    检索）+ 可选 embedding_model 匹配；排序：余弦距离升序，取前 top_k 条；
    每行带出所属文档标题（供草稿上下文标注知识出处）。
    """
    distance = KbChunk.embedding.cosine_distance(query_embedding)
    stmt = (
        select(KbChunk, KbDocument.title, distance)
        .join(KbDocument, KbChunk.document_id == KbDocument.id)
        .where(
            KbChunk.kb_type == kb_type,
            # 检索只取 active 文档的块：归档下线的知识不再命中
            KbDocument.status == KB_STATUS_ACTIVE,
        )
        .order_by(distance)
        .limit(top_k)
    )
    if embedding_model is not None:
        stmt = stmt.where(KbChunk.embedding_model == embedding_model)
    return stmt


class KbDocumentRepository:
    """kb_documents 表的异步访问入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_kb_document_by_id(self, document_id: int) -> KbDocument | None:
        """按主键查询单个知识文档。"""
        return await self.session.get(KbDocument, document_id)

    async def get_kb_document_by_source_key(self, source_key: str) -> KbDocument | None:
        """按来源幂等键查询：重入库时先查它决定 insert / update / skip。"""
        stmt = select(KbDocument).where(KbDocument.source_key == source_key)
        return await self.session.scalar(stmt)

    async def list_kb_document_by_kb_type(
        self, kb_type: str, *, active_only: bool = False
    ) -> list[KbDocument]:
        """按知识类型查询文档列表，默认返回全部；active_only=True 时仅返回生效文档。"""
        _validate_kb_type(kb_type)

        stmt = select(KbDocument).where(KbDocument.kb_type == kb_type).order_by(KbDocument.id)
        if active_only:
            stmt = stmt.where(KbDocument.status == KB_STATUS_ACTIVE)

        result = await self.session.scalars(stmt)
        return list(result.all())

    async def create_kb_document(self, entity: KbDocument) -> KbDocument:
        """插入单个知识文档，flush 后返回带主键的实体。"""
        if not isinstance(entity, KbDocument):
            msg = f"expected KbDocument, got {type(entity).__name__}"
            raise TypeError(msg)

        self.session.add(entity)
        await self.session.flush()
        return entity

    async def update_kb_document_by_id(
        self,
        document_id: int,
        *,
        title: str | None = None,
        content_hash: str | None = None,
        status: str | None = None,
    ) -> bool:
        """按主键更新文档的可变字段（重入库改 hash、归档改 status），返回是否命中。

        仅更新显式传入的字段；updated_at 随行刷新，与既有仓储的更新方法一致。
        """
        values: dict = {"updated_at": datetime.now(UTC)}
        if title is not None:
            if not title:
                msg = "title must be non-empty when provided"
                raise ValueError(msg)
            values["title"] = title
        if content_hash is not None:
            if not content_hash:
                msg = "content_hash must be non-empty when provided"
                raise ValueError(msg)
            values["content_hash"] = content_hash
        if status is not None:
            if status not in {"active", "archived"}:
                msg = f"status must be one of ['active', 'archived'], got {status!r}"
                raise ValueError(msg)
            values["status"] = status

        stmt = update(KbDocument).where(KbDocument.id == document_id).values(**values)
        result = await self.session.execute(stmt)
        return bool(result.rowcount)

    async def delete_kb_document_by_id(self, document_id: int) -> bool:
        """按主键删除文档，返回是否删除成功。

        块表无物理级联（与既有表一致的逻辑外键约定），删除文档前由调用方
        先经 ``KbChunkRepository.delete_kb_chunk_by_document_id`` 清块。
        """
        stmt = delete(KbDocument).where(KbDocument.id == document_id)
        result = await self.session.execute(stmt)
        return bool(result.rowcount)


class KbChunkRepository:
    """kb_chunks 表的异步访问入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_kb_chunk_by_id(self, chunk_id: int) -> KbChunk | None:
        """按主键查询单个分块。"""
        return await self.session.get(KbChunk, chunk_id)

    async def list_kb_chunk_by_document_id(self, document_id: int) -> list[KbChunk]:
        """按所属文档查询分块列表，按 chunk_index 排序（可按序还原上下文）。"""
        stmt = (
            select(KbChunk).where(KbChunk.document_id == document_id).order_by(KbChunk.chunk_index)
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_kb_chunk_by_kb_type(
        self, kb_type: str, *, active_only: bool = True
    ) -> list[KbChunk]:
        """按知识类型全量列出分块（非向量），按文档与块内序号稳定排序。

        供"恒在场"类知识（如合规红线）全量读取注入 prompt，不经相似度召回；
        active_only=True 时仅返回生效文档的块（归档知识不参与注入）。
        """
        _validate_kb_type(kb_type)

        stmt = (
            select(KbChunk)
            .where(KbChunk.kb_type == kb_type)
            .order_by(KbChunk.document_id, KbChunk.chunk_index)
        )
        if active_only:
            stmt = stmt.where(
                KbChunk.document_id.in_(
                    select(KbDocument.id).where(KbDocument.status == KB_STATUS_ACTIVE)
                )
            )

        result = await self.session.scalars(stmt)
        return list(result.all())

    async def create_kb_chunk(self, entity: KbChunk) -> KbChunk:
        """插入单个分块，flush 后返回带主键的实体。"""
        if not isinstance(entity, KbChunk):
            msg = f"expected KbChunk, got {type(entity).__name__}"
            raise TypeError(msg)

        self.session.add(entity)
        await self.session.flush()
        return entity

    async def bulk_create_kb_chunk(self, entities: list[KbChunk]) -> list[KbChunk]:
        """批量插入分块（整篇换块的新块写入），flush 后按序返回带主键的实体列表。

        ORM add_all + 一次 flush：单事务批量落库，且 sqlite 单测可正常执行。
        """
        for entity in entities:
            if not isinstance(entity, KbChunk):
                msg = f"expected KbChunk, got {type(entity).__name__}"
                raise TypeError(msg)

        self.session.add_all(entities)
        await self.session.flush()
        return list(entities)

    async def delete_kb_chunk_by_document_id(self, document_id: int) -> int:
        """删除文档下的全部分块（整篇换块第一步），返回删除条数。"""
        stmt = delete(KbChunk).where(KbChunk.document_id == document_id)
        result = await self.session.execute(stmt)
        return result.rowcount

    async def list_kb_chunk_by_similarity(
        self,
        kb_type: str,
        query_embedding: list[float],
        *,
        top_k: int = 5,
        embedding_model: str | None = None,
    ) -> list[KbChunkSimilarityRow]:
        """按余弦距离检索某知识类型下最相近的 top_k 个分块。

        过滤条件：kb_type 精确匹配 + 所属文档 status=active（归档知识不参与检索）
        + 可选 embedding_model 匹配（不同模型的向量不可比，生产检索必须传）。
        返回 (分块, 文档标题, 余弦距离) 行列表，按距离升序；距离越小越相似，
        标题供调用方在草稿上下文与来源核对中标注知识出处。
        """
        _validate_kb_type(kb_type)
        if not isinstance(top_k, int) or top_k <= 0:
            msg = f"top_k must be positive int, got {top_k!r}"
            raise ValueError(msg)
        if (
            not isinstance(query_embedding, list)
            or len(query_embedding) != KB_EMBEDDING_DIMENSIONS
            or not all(isinstance(x, (int, float)) for x in query_embedding)
        ):
            got = (
                len(query_embedding) if isinstance(query_embedding, list) else repr(query_embedding)
            )
            msg = (
                f"query_embedding must be list of {KB_EMBEDDING_DIMENSIONS} floats, "
                f"got length {got}"
            )
            raise ValueError(msg)

        stmt = _build_similarity_stmt(
            kb_type,
            query_embedding,
            top_k=top_k,
            embedding_model=embedding_model,
        )

        rows = await self.session.execute(stmt)
        return [
            KbChunkSimilarityRow(chunk=chunk, document_title=title, distance=float(dist))
            for chunk, title, dist in rows.all()
        ]
