from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

from app.providers.email.base import MailClientError
from app.providers.email.factory import create_client
from app.schemas import AccountSpec, EmailData, RawEmail
from app.services.parsing import parse_email

logger = logging.getLogger(__name__)


class EmailService:
    """邮件接收服务：阻塞式接收新邮件并解析，不做任何 DB 操作。

    长驻的 IMAP IDLE 调用占用执行器线程；解析与落库的编排由调用方通过
    ``on_batch`` 异步回调完成，本类只负责线程↔事件循环的桥接与背压。
    """

    def __init__(self, client_factory=create_client, parser=parse_email) -> None:
        self._client_factory = client_factory
        self._parser = parser

    async def receive(
        self,
        account: AccountSpec,
        on_batch: Callable[[list[EmailData]], Awaitable[None]],
        stop_event: threading.Event,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        """阻塞式接收指定账号的新邮件，直到 stop_event 置位或认证失败。

        阻塞的 IMAP 调用通过执行器线程长驻运行，避免阻塞事件循环；新邮件批次
        解析后交给 ``on_batch``（在事件循环中执行），并阻塞等待其完成——落库
        失败会原样传回 provider，本批不确认，之后自动重推（落库需幂等）。
        单封解析失败仅告警并跳过，不中断整批。
        """
        loop = asyncio.get_running_loop()

        def _blocking_receive() -> None:
            # 客户端对象只在该线程内使用，避免跨线程复用 IMAP 连接
            client = self._client_factory(account)
            try:
                client.connect()
                client.receive_emails(
                    account.folder,
                    self._make_sync_callback(account, on_batch, loop),
                    stop_event=stop_event,
                )
            finally:
                with contextlib.suppress(Exception):
                    client.close()

        try:
            await loop.run_in_executor(executor, _blocking_receive)
        except MailClientError:
            # 不可恢复错误（如认证失败）向上抛出，交由上层（core）记录隔离
            raise

    def _make_sync_callback(
        self,
        account: AccountSpec,
        on_batch: Callable[[list[EmailData]], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
    ) -> Callable[[list[tuple[int, bytes]]], None]:
        """把异步批次回调包装为接收线程内的同步回调（带背压）。"""

        def _on_raw_batch(raw_list: list[tuple[int, bytes]]) -> None:
            messages: list[EmailData] = []
            for uid, raw in raw_list:
                try:
                    messages.append(
                        self._parser(RawEmail(account_id=account.account_id, uid=uid, raw=raw))
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("parse failed for account %s uid %s: %s", account.name, uid, exc)
                    continue

            # 阻塞等待事件循环完成落库：异常原样传回 provider（不确认本批）
            asyncio.run_coroutine_threadsafe(on_batch(messages), loop).result()

        return _on_raw_batch
