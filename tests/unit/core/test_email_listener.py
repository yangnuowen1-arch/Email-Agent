"""core.listener.EmailListener 单元测试：编排接收+落库，账号级失败隔离。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from app.core.listener import EmailListener
from app.core.settings import AppConfig
from app.db.db import Account
from app.schemas import EmailData


@asynccontextmanager
async def _fake_session_cm(session):
    yield session


class FakeDatabase:
    def __init__(self, session):
        self._session = session

    def session(self):
        return _fake_session_cm(self._session)


class FakeEmailService:
    """按 account_id 回放预设批次；值可为异常以模拟接收失败（如认证失败）。"""

    def __init__(self, per_account):
        self.per_account = per_account

    async def receive(self, spec, on_batch, stop_event, executor=None):
        value = self.per_account[spec.account_id]
        if isinstance(value, Exception):
            raise value
        for messages in value:
            if stop_event.is_set():
                return
            await on_batch(messages)


def _config() -> AppConfig:
    return AppConfig(database_url="sqlite+aiosqlite://")


def _account(account_id=1, last_sync_uid=5) -> Account:
    return Account(
        id=account_id,
        name=f"acc{account_id}",
        host="imap.example.com",
        username="u@example.com",
        password="secret",
        last_sync_uid=last_sync_uid,
    )


def _messages(*uids: int) -> list[EmailData]:
    return [EmailData(account_id=1, uid=u, subject=f"s{u}") for u in uids]


def _mock_session(accounts):
    session = AsyncMock()
    # scalars() 返回 MagicMock（其 .all() 给出账号列表）；注意 AsyncMock 的
    # return_value 本身是 AsyncMock，会令 .all() 变成协程导致无法迭代，故显式用 MagicMock
    scalars_result = MagicMock(all=MagicMock(return_value=accounts))
    session.scalars = AsyncMock(return_value=scalars_result)
    # execute() 返回 MagicMock（其 .fetchall() 给出插入行数）
    execute_result = MagicMock(fetchall=MagicMock(return_value=[(1,), (2,)]))
    session.execute = AsyncMock(return_value=execute_result)
    return session


async def test_listener_persists_and_advances_checkpoint():
    account = _account(last_sync_uid=5)
    session = _mock_session([account])
    listener = EmailListener(
        FakeDatabase(session), FakeEmailService({1: [_messages(6, 7, 8)]}), _config()
    )

    await listener.run()
    await listener.stop()

    # bulk_create 一次 + 断点推进一次 = 两次 execute；max_uid=8 > 5 → 推进断点
    assert session.execute.await_count == 2


async def test_listener_does_not_regress_checkpoint():
    account = _account(last_sync_uid=5)
    session = _mock_session([account])
    listener = EmailListener(
        FakeDatabase(session), FakeEmailService({1: [_messages(3)]}), _config()
    )

    await listener.run()
    await listener.stop()

    # max_uid=3 <= last_sync_uid=5：仅入库，不推进断点
    assert session.execute.await_count == 1


async def test_listener_isolates_per_account_failure():
    accounts = [_account(1), _account(2)]
    session = _mock_session(accounts)
    service = FakeEmailService({1: [_messages(6)], 2: RuntimeError("auth boom")})
    listener = EmailListener(FakeDatabase(session), service, _config())

    # 账号 2 的不可恢复错误只记日志退出，不拖累账号 1，也不向上抛
    await listener.run()
    await listener.stop()

    assert session.execute.await_count == 2  # 仅账号 1：bulk + checkpoint


async def test_listener_empty_messages_does_not_write():
    account = _account()
    session = _mock_session([account])
    listener = EmailListener(FakeDatabase(session), FakeEmailService({1: []}), _config())

    await listener.run()
    await listener.stop()

    # 无邮件批次时不打开存储事务、不执行写入
    session.execute.assert_not_awaited()


async def test_listener_no_enabled_accounts_returns_immediately():
    session = _mock_session([])
    listener = EmailListener(FakeDatabase(session), FakeEmailService({}), _config())

    # 无启用账号：直接返回，未创建线程池与任务
    await listener.run()
    await listener.stop()

    assert listener._executor is None
    assert listener._tasks == []
