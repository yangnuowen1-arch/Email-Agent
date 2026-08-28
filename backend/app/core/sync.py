from __future__ import annotations

import asyncio
import logging
import time

from app.db.db import Account, EmailMessage
from app.db.repositories import EmailAccountRepository, EmailRepository
from app.schemas import AccountResult, AccountSpec, BatchResult, EmailData

logger = logging.getLogger(__name__)


def _to_email_message(email: EmailData) -> EmailMessage:
    """把传输层 EmailData 投影为持久化层 EmailMessage（落库即未读）。"""
    return EmailMessage(
        account_id=email.account_id,
        uid=email.uid,
        message_id=email.message_id,
        subject=email.subject,
        sender=email.sender,
        recipients=email.recipients,
        sent_at=email.sent_at,
        text_body=email.text_body,
        html_body=email.html_body,
        fetched_at=email.fetched_at,
        is_read=False,
    )


def _to_account_spec(account: Account) -> AccountSpec:
    """把 ORM Account 投影为服务层所需的只读视图（解耦 services 与 db）。"""
    return AccountSpec(
        account_id=account.id,
        name=account.name,
        last_sync_uid=account.last_sync_uid,
        host=account.host,
        username=account.username,
        password=account.password,
        port=account.port,
        protocol=account.protocol,
        use_ssl=account.use_ssl,
        folder=account.folder,
    )


class EmailSynchronizer:
    """邮件同步器：读取各启用账号的邮件（经 services）并原子落库。

    本类是唯一允许执行 DB 写操作的地方；读邮件委托给 ``email_reader``（EmailService），
    自身只负责事务边界、并发控制、断点推进与失败隔离。
    """

    def __init__(self, database, email_reader, config, max_workers=None, timeout=None) -> None:
        self._database = database
        self._reader = email_reader
        self._config = config
        self._max_workers = max_workers or config.sync_max_workers
        self._timeout = timeout or config.sync_timeout_seconds

    async def sync_accounts(self, *, full: bool = False, limit: int | None = 10) -> BatchResult:
        """拉取所有启用账号的邮件并落库，返回批量汇总报告。"""
        start = time.monotonic()
        # 1. 从 DB 读取启用账号配置
        async with self._database.session() as session:
            accounts = await EmailAccountRepository(session).list_account(enabled_only=True)
        specs = [_to_account_spec(a) for a in accounts]

        # 2. 账号级并发，受信号量限制，避免瞬间打满连接池
        sem = asyncio.Semaphore(self._max_workers)
        results = await asyncio.gather(*[self._sync_one(sp, sem, full, limit) for sp in specs])

        # 3. 按 account_id 排序，保证结果顺序稳定
        results.sort(key=lambda r: r.account_id)

        return BatchResult(
            results=list(results),
            total_inserted=sum(r.inserted for r in results),
            total_skipped=sum(r.skipped for r in results),
            total_failed=sum(1 for r in results if r.error is not None),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def _sync_one(self, spec: AccountSpec, sem, full, limit) -> AccountResult:
        """单账号：读邮件 → 落库；异常隔离，不拖累其他账号。"""
        async with sem:
            try:
                messages = await asyncio.wait_for(
                    self._read_messages(spec, full, limit), timeout=self._timeout
                )
                return await self._store_messages(spec, messages, limit)
            except Exception as exc:  # noqa: BLE001
                logger.error("sync failed for %s (id=%s): %s", spec.name, spec.account_id, exc)
                return AccountResult(account_id=spec.account_id, name=spec.name, error=str(exc))

    async def _read_messages(self, spec: AccountSpec, full, limit) -> list[EmailData]:
        """委托 services 读取该账号的邮件内容（此处不含任何 DB 操作）。"""
        return await self._reader.read(spec, full=full, limit=limit)

    async def _store_messages(
        self, spec: AccountSpec, messages: list[EmailData], limit
    ) -> AccountResult:
        """把读到的邮件原子落库，并按需推进账号增量断点。"""
        start = time.monotonic()
        if not messages:
            return AccountResult(account_id=spec.account_id, name=spec.name, duration_ms=0)

        # 单账号事务：邮件入库 + 断点推进随一次提交原子生效
        async with self._database.session() as session:
            inserted = await EmailRepository(session).bulk_create_email(
                [_to_email_message(m) for m in messages]
            )
            skipped = len(messages) - inserted
            max_uid = max((m.uid for m in messages), default=spec.last_sync_uid)
            # 限量模式不推进断点（便于反复调试）；有新 UID 才推进
            if limit is None and max_uid > spec.last_sync_uid:
                await EmailAccountRepository(session).update_account_checkpoint(
                    spec.account_id, max_uid
                )

        return AccountResult(
            account_id=spec.account_id,
            name=spec.name,
            inserted=inserted,
            skipped=skipped,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
