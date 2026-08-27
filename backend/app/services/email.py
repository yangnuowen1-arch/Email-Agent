from __future__ import annotations

import asyncio
import contextlib
import logging

from app.providers.email.base import MailClientError
from app.providers.email.factory import create_client
from app.schemas import AccountSpec, EmailData, RawEmail
from app.services.parsing import parse_email

logger = logging.getLogger(__name__)


def _blocking_fetch(account: AccountSpec, client_factory, since_uid: int, limit):
    """在线程池内同步拉取原始邮件：连接→拉取→关闭。

    客户端对象只在该线程内使用，避免跨线程复用 IMAP 连接。
    """
    client = client_factory(account)
    try:
        client.connect()
        return client.fetch_emails(account.folder, since_uid, limit=limit)
    finally:
        with contextlib.suppress(Exception):
            client.close()


class EmailService:
    """邮件读取服务：仅负责从邮件源拉取并解析邮件内容，不做任何 DB 操作。"""

    def __init__(self, client_factory=create_client, parser=parse_email) -> None:
        self._client_factory = client_factory
        self._parser = parser

    async def read(
        self,
        account: AccountSpec,
        *,
        full: bool = False,
        limit: int | None = None,
    ) -> list[EmailData]:
        """拉取并解析指定账号的邮件，返回解析后的邮件数据列表。

        阻塞的 IMAP 调用通过事件循环的默认线程池执行，避免阻塞主循环；解析失败
        的单封邮件仅告警并跳过，不中断整批。
        """
        if limit is not None and (not isinstance(limit, int) or limit <= 0):
            msg = f"limit must be positive int or None, got {limit!r}"
            raise ValueError(msg)

        # 增量起点：full 模式从 0 开始（全量），否则从上次断点继续
        since_uid = 0 if full else account.last_sync_uid

        loop = asyncio.get_running_loop()
        try:
            raw_list = await loop.run_in_executor(
                None, _blocking_fetch, account, self._client_factory, since_uid, limit
            )
        except MailClientError:
            # 连接/拉取失败向上抛出，交由上层（core）做账号级隔离
            raise

        messages: list[EmailData] = []
        for uid, raw in raw_list:
            try:
                messages.append(
                    self._parser(RawEmail(account_id=account.account_id, uid=uid, raw=raw))
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("parse failed for account %s uid %s: %s", account.name, uid, exc)
                continue
        return messages
