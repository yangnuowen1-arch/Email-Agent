"""异步数据访问层：一个表一个 Repository。

所有 Repository 的会话由构造函数注入，事务边界由 ``Database.session()`` 管理。
Repository 内部不提交事务，仅通过 ``flush()`` 获取主键。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import ParsedEmail

from .db import Account, EmailAnalysis, EmailMessage


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

    async def bulk_create_email(self, messages: list[EmailMessage]) -> int:
        """批量插入邮件，利用 (account_id, uid) 幂等去重，返回实际插入行数。"""
        if not messages:
            return 0

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
            .returning(EmailMessage.id)
        )
        result = await self.session.execute(stmt)
        return len(result.fetchall())

    async def delete_email_by_id(self, email_id: int) -> bool:
        """按主键删除邮件，返回是否删除成功。"""
        stmt = delete(EmailMessage).where(EmailMessage.id == email_id)
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
