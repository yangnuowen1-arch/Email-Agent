from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import structlog

from app.db.db import Account, EmailMessage
from app.db.repositories import EmailAccountRepository, EmailRepository
from app.schemas import AccountSpec, EmailData
from app.services.email import EmailService

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


class EmailListener:
    """邮件监听器：为每个启用账号维持一个长驻接收线程（一账号一线程）。

    本类是唯一允许执行 DB 写操作的地方；阻塞接收委托给 ``email_service``
    （线程内 IMAP IDLE），自身负责账号发现、任务编排、落库事务与断点推进。
    生命周期由容器统一接管：``run`` 阻塞直到停止，``stop`` 幂等可重复调用。
    """

    def __init__(self, database, email_service: EmailService, config, logger=None) -> None:
        self._database = database
        self._email = email_service
        self._config = config
        self._logger = logger or structlog.get_logger("email-agent.listener")

        # 运行期状态：账号 stop_event、监听任务与专用线程池，仅在 run 期间有效
        self._stop_events: dict[int, threading.Event] = {}
        self._tasks: list[asyncio.Task] = []
        self._executor: ThreadPoolExecutor | None = None

    async def run(self) -> None:
        """为所有启用账号启动长驻监听，阻塞直到全部任务结束。

        正常由 ``stop`` 触发退出；账号任务的不可恢复错误（如认证失败）只记
        日志并退出该账号的监听，不影响其他账号。结束时自动清理线程池。
        """
        specs = await self._load_account_specs()
        if not specs:
            self._logger.warning("listener_no_enabled_accounts")
            return

        # 一账号一线程：IDLE 挂起需独占连接与线程，线程数随启用账号数走
        self._executor = ThreadPoolExecutor(
            max_workers=len(specs), thread_name_prefix="imap-listen"
        )
        for spec in specs:
            stop_event = threading.Event()
            self._stop_events[spec.account_id] = stop_event
            self._tasks.append(
                asyncio.create_task(
                    self._listen_one(spec, stop_event),
                    name=f"listen-account-{spec.account_id}",
                )
            )

        self._logger.info("listener_started", accounts=[spec.name for spec in specs])
        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.stop()

        self._logger.info("listener_finished")

    def request_stop(self) -> None:
        """请求停止全部监听（线程安全，可在信号处理器中调用）。

        各接收线程在当前等待周期内尽快退出；完整清理见 :meth:`stop`。
        """
        for stop_event in self._stop_events.values():
            stop_event.set()

    async def stop(self) -> None:
        """停止全部监听并释放线程池；幂等，容器释放路径统一调用。"""
        self.request_stop()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._stop_events.clear()

        if self._executor is not None:
            # 接收线程在置位 stop_event 后于当前 ping 周期内退出
            self._executor.shutdown(wait=True)
            self._executor = None

    async def _listen_one(self, spec: AccountSpec, stop_event: threading.Event) -> None:
        """单账号长驻监听：接收（线程内阻塞）→ 解析 → 原子落库。"""
        try:
            await self._email.receive(
                spec,
                self._make_store_callback(spec),
                stop_event,
                executor=self._executor,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 不可恢复错误（认证失败等）：记录并退出该账号监听，账号级隔离
            self._logger.error(
                "listener_account_failed",
                account_id=spec.account_id,
                account_name=spec.name,
                error=str(exc),
            )

    def _make_store_callback(self, spec: AccountSpec):
        """构造单账号落库回调：邮件入库 + 断点推进在单账号事务内原子完成。"""

        async def _store(messages: list[EmailData]) -> None:
            if not messages:
                return

            start = time.monotonic()
            # 单账号事务：邮件入库 + 断点推进随一次提交原子生效
            async with self._database.session() as session:
                inserted = await EmailRepository(session).bulk_create_email(
                    [_to_email_message(m) for m in messages]
                )
                max_uid = max(message.uid for message in messages)
                if max_uid > spec.last_sync_uid:
                    await EmailAccountRepository(session).update_account_checkpoint(
                        spec.account_id, max_uid
                    )

            self._logger.info(
                "listener_batch_stored",
                account_id=spec.account_id,
                account_name=spec.name,
                received=len(messages),
                inserted=inserted,
                skipped=len(messages) - inserted,
                max_uid=max_uid,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        return _store

    async def _load_account_specs(self) -> list[AccountSpec]:
        """读取启用账号并投影为只读视图。"""
        async with self._database.session() as session:
            accounts = await EmailAccountRepository(session).list_account(enabled_only=True)
        return [_to_account_spec(account) for account in accounts]
