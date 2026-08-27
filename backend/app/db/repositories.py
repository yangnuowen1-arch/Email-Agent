"""异步数据访问层：一个表一个 Repository。

所有 Repository 的会话由构造函数注入，事务边界由 ``Database.session()`` 管理。
Repository 内部不提交事务，仅通过 ``flush()`` 获取主键。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Account, EmailMessage


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
        """单调推进账号的增量断点，并刷新最近成功同步时间。"""
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
            .values(
                # Concurrent sync processes must never overwrite a newer cursor
                # with an older one read at the beginning of their own run.
                last_sync_uid=case(
                    (Account.last_sync_uid > last_sync_uid, Account.last_sync_uid),
                    else_=last_sync_uid,
                ),
                last_sync_at=last_sync_at,
            )
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
        stmt = select(EmailMessage).where(
            EmailMessage.account_id == account_id,
        ).order_by(EmailMessage.id)
        result = await self.session.scalars(stmt)
        return list(result.all())

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
            values.append({
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
            })

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
