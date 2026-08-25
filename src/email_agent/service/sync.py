from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from email_agent.email_clients.base import MailClientError
from email_agent.email_clients.factory import create_client
from email_agent.models.account import Account
from email_agent.parsing.parser import parse_email
from email_agent.repository.email_accounts import AccountStore
from email_agent.repository.emails import EmailStore

# 模块级日志记录器，用于记录解析失败、同步失败等信息
logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SyncResult:
    """单账号同步结果，用于汇总报告和上层判断。"""

    # 账号标识
    account_id: int
    name: str
    # 各阶段计数：拉取/解析/入库/跳过（幂等去重）
    fetched: int
    parsed: int
    inserted: int
    skipped: int
    # 本次同步后应记录的最大 UID（用于断点推进）
    max_uid: int
    # 错误信息，None 表示成功
    error: str | None
    # 耗时（毫秒）
    duration_ms: int


def sync_account(
    account: Account,
    *,
    session: Any,
    email_store: EmailStore,
    checkpoint_store: AccountStore,
    client_factory: Callable[[Account], Any] = create_client,
    parser: Callable[[bytes, int, int], Any] = parse_email,
    limit: int | None = None,
    full: bool = False,
    now: datetime | None = None,
) -> SyncResult:
    """同步单个账号：建客户端→增量拉取→解析→批量入库→回写断点。

    纯编排逻辑，DB 操作通过按业务区分的 Store 显式注入。``session`` 与两个 Store
    **共享同一条连接、同一个事务**：所有写操作（邮件入库 + 断点推进）由一次
    ``session.commit()`` 原子提交，保证多表一致性；任意失败则一次 ``session.rollback()``
    全部回滚。``limit`` 模式下不推进断点，专为调试/首跑限量设计。
    """
    # 校验 limit 参数，None 表示不限量
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        msg = f"limit must be positive int or None, got {limit!r}"
        raise ValueError(msg)

    # 记录开始时间，用于计算耗时
    start = time.monotonic()
    fetched = 0
    parsed = 0
    inserted = 0
    skipped = 0
    # 初始 max_uid 为账号原有断点，若无新邮件则保持不变
    max_uid = account.last_sync_uid
    error: str | None = None
    client: Any | None = None

    try:
        # 确定增量起点：full 模式强制从 0 开始（全量），否则从上次断点继续
        since_uid = 0 if full else account.last_sync_uid

        # 创建并连接邮件客户端，client_factory 默认为 create_client，测试可注入 Fake
        client = client_factory(account)
        client.connect()  # type: ignore[union-attr]

        # 拉取原始邮件，folder 和 since_uid 决定增量范围，limit 控制调试时的截断
        raw_list = client.fetch_emails(account.folder, since_uid, limit=limit)  # type: ignore[union-attr]
        fetched = len(raw_list)

        # 无新邮件时显式回滚并返回，避免空事务提交
        if not raw_list:
            session.rollback()
            duration_ms = int((time.monotonic() - start) * 1000)
            return SyncResult(
                account_id=account.id,
                name=account.name,
                fetched=fetched,
                parsed=parsed,
                inserted=inserted,
                skipped=skipped,
                max_uid=max_uid,
                error=None,
                duration_ms=duration_ms,
            )

        # 逐封解析原始字节为领域模型，记录批次内最大 UID 用于断点推进
        messages: list[Any] = []
        max_uid_in_batch = since_uid
        for uid, raw in raw_list:
            try:
                msg = parser(raw, account.id, uid)
                messages.append(msg)
                if uid > max_uid_in_batch:
                    max_uid_in_batch = uid
            except Exception as exc:  # noqa: BLE001
                # 单封解析失败仅告警并跳过，不中断整批处理
                logger.warning("parse failed for account %s uid %s: %s", account.name, uid, exc)
                continue

        parsed = len(messages)

        # 全部解析失败时同样显式回滚并返回
        if not messages:
            session.rollback()
            duration_ms = int((time.monotonic() - start) * 1000)
            return SyncResult(
                account_id=account.id,
                name=account.name,
                fetched=fetched,
                parsed=parsed,
                inserted=inserted,
                skipped=skipped,
                max_uid=max_uid,
                error=None,
                duration_ms=duration_ms,
            )

        # 批量入库，利用 ON CONFLICT 实现幂等，返回实际插入数（业务：EmailStore）
        inserted = email_store.bulk_insert(messages)
        # 跳过数 = 解析数 - 插入数，即因幂等去重而未插入的数量
        skipped = parsed - inserted

        # 判断是否应推进断点：限量模式下不推进（便于反复调试），否则有新 UID 才推进
        should_update = (limit is None) and (max_uid_in_batch > account.last_sync_uid)
        # 本次最大 UID 用于汇总报告（限量模式也照常展示，只是不写回断点）
        max_uid = max_uid_in_batch
        if should_update:
            # 推进断点并刷新同步时间，now 参数可注入固定时间以便测试（业务：AccountStore）
            ts = now or datetime.now(UTC)
            checkpoint_store.update_checkpoint(account.id, max_uid_in_batch, ts)

        # 无论是否推进断点，本账号的所有写操作（邮件 + 断点）随一次提交原子生效
        session.commit()

    except MailClientError as exc:
        # 邮件客户端异常（连接/拉取失败），记录错误并显式回滚本事务
        error = str(exc)
        logger.error("sync failed for account %s (id=%s): %s", account.name, account.id, error)
        session.rollback()
    except Exception as exc:  # noqa: BLE001
        # 其他未预期异常同样隔离，记录后回滚，避免影响其他账号
        error = str(exc)
        logger.error("sync failed for account %s (id=%s): %s", account.name, account.id, error)
        session.rollback()
    finally:
        # 无论成功失败，都尝试关闭客户端，释放 IMAP 连接
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()  # type: ignore[union-attr]
        # 计算总耗时
        duration_ms = int((time.monotonic() - start) * 1000)

    # 返回同步结果，供上层汇总
    return SyncResult(
        account_id=account.id,
        name=account.name,
        fetched=fetched,
        parsed=parsed,
        inserted=inserted,
        skipped=skipped,
        max_uid=max_uid,
        error=error,
        duration_ms=duration_ms,
    )


def sync_all(
    accounts: list[Account],
    *,
    session_factory: Callable[[], Any],
    max_workers: int,
    timeout: float | int | None,
    limit: int | None = None,
    full: bool = False,
    client_factory: Callable[[Account], Any] = create_client,
    parser: Callable[[bytes, int, int], Any] = parse_email,
    now: datetime | None = None,
) -> list[SyncResult]:
    """并发同步多个账号，具备失败隔离和超时控制。"""
    # 空账号列表直接返回，避免创建无意义的线程池
    if not accounts:
        return []

    # 校验并发数参数
    if not isinstance(max_workers, int) or max_workers < 1:
        msg = f"max_workers must be >=1, got {max_workers!r}"
        raise ValueError(msg)

    results: list[SyncResult] = []
    # 记录 Future 到账号的映射，用于超时/异常时定位是哪个账号失败
    future_to_account: dict[concurrent.futures.Future, Account] = {}

    def _task(acc: Account) -> SyncResult:
        """单账号任务闭包：获取随线程独占的 Session→执行同步→归还 Session。"""
        # 每个线程独立获取 Session，避免跨线程共享连接导致竞态；
        # 同一账号内的 EmailStore 与 AccountStore 共享该 Session，事务原子
        session = session_factory()
        email_store = EmailStore(session)
        checkpoint_store = AccountStore(session)
        try:
            return sync_account(
                acc,
                session=session,
                email_store=email_store,
                checkpoint_store=checkpoint_store,
                client_factory=client_factory,
                parser=parser,
                limit=limit,
                full=full,
                now=now,
            )
        finally:
            # 归还 Session（关闭连接），事务已在 sync_account 内提交或回滚
            with contextlib.suppress(Exception):
                session.close()

    # 创建线程池并发执行，I/O 密集场景下线程池优于进程池
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for acc in accounts:
            fut = executor.submit(_task, acc)
            future_to_account[fut] = acc

        # 逐个等待结果，处理超时和异常，实现账号级失败隔离
        for fut, acc in future_to_account.items():
            try:
                res = (  # noqa: SIM108
                    fut.result(timeout=timeout) if timeout is not None else fut.result()
                )
                results.append(res)
            except concurrent.futures.TimeoutError:
                # 超时：记录错误日志，返回带 timeout 错误的结果
                logger.error(
                    "sync timeout for account %s (id=%s) after %s s",
                    acc.name,
                    acc.id,
                    timeout,
                )
                results.append(
                    SyncResult(
                        account_id=acc.id,
                        name=acc.name,
                        fetched=0,
                        parsed=0,
                        inserted=0,
                        skipped=0,
                        max_uid=acc.last_sync_uid,
                        error=f"timeout after {timeout}s",
                        duration_ms=int(float(timeout) * 1000) if timeout else 0,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # 其他异常同样隔离，记录后继续处理其他账号
                logger.error("sync failed for account %s (id=%s): %s", acc.name, acc.id, exc)
                results.append(
                    SyncResult(
                        account_id=acc.id,
                        name=acc.name,
                        fetched=0,
                        parsed=0,
                        inserted=0,
                        skipped=0,
                        max_uid=acc.last_sync_uid,
                        error=str(exc),
                        duration_ms=0,
                    )
                )

    # 按 account_id 排序，保证结果顺序稳定，便于测试和展示
    results.sort(key=lambda r: r.account_id)
    return results
