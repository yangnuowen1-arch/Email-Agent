from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.account import Account


class AccountStore:
    """email_accounts 表的访问入口：绑定到一个 Session（即一个事务/连接）。

    连接与事务生命周期由 Session 负责，本类只暴露业务方法，
    不再持有连接、也不自管 commit/rollback/close。
    """

    def __init__(self, session: Session) -> None:
        # 绑定调用方提供的 Session（同一线程/账号内与 EmailStore 共享，保证事务原子性）
        self._s = session

    def get_enabled_accounts(self) -> list[Account]:
        """查询所有启用的邮箱账号，按 id 升序返回。"""
        # 只查询 enabled=TRUE 的账号，这是程序的唯一输入源
        # ORDER BY id 保证每次调度顺序稳定，便于日志追踪和测试断言
        stmt = select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
        try:
            return list(self._s.scalars(stmt).all())
        except Exception as exc:
            # 查询失败时包装异常，带上上下文便于排查 SQL 或连接问题
            raise RuntimeError(f"failed to fetch enabled accounts: {exc}") from exc

    def update_checkpoint(
        self,
        account_id: int,
        last_sync_uid: int,
        last_sync_at: datetime | None = None,
    ) -> None:
        """推进账号的增量断点（last_sync_uid / last_sync_at）。"""
        # 参数校验：account_id 必须为正整数，last_sync_uid 必须为非负整数
        if not isinstance(account_id, int) or account_id <= 0:
            msg = f"account_id must be positive int, got {account_id!r}"
            raise ValueError(msg)
        if not isinstance(last_sync_uid, int) or last_sync_uid < 0:
            msg = f"last_sync_uid must be int >=0, got {last_sync_uid!r}"
            raise ValueError(msg)
        # 如果调用方未传入时间，默认使用当前 UTC 时间
        if last_sync_at is None:
            last_sync_at = datetime.now(UTC)
        # 更新断点和时间戳，断点用于下次增量拉取的 since_uid
        stmt = (
            update(Account)
            .where(Account.id == account_id)
            .values(last_sync_uid=last_sync_uid, last_sync_at=last_sync_at)
        )
        try:
            self._s.execute(stmt)
        except Exception as exc:
            msg = f"failed to update checkpoint for account {account_id}: {exc}"
            raise RuntimeError(msg) from exc
