"""core.sync.EmailSynchronizer 单元测试：编排读+落库，账号级失败隔离。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from app.core.settings import AppConfig
from app.core.sync import EmailSynchronizer
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


class FakeReader:
    """按 account_id 返回邮件列表；值可为异常以模拟读取失败。"""

    def __init__(self, per_account):
        self.per_account = per_account
        self.calls = []

    async def read(self, spec, *, full=False, limit=None):
        self.calls.append((spec.account_id, full, limit))
        value = self.per_account[spec.account_id]
        if isinstance(value, Exception):
            raise value
        return value


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


async def test_sync_accounts_persists_and_advances_checkpoint():
    account = _account(last_sync_uid=5)
    session = _mock_session([account])
    reader = FakeReader({1: _messages(6, 7, 8)})  # max_uid=8 > 5 → 推进断点
    synchronizer = EmailSynchronizer(FakeDatabase(session), reader, _config())

    # limit=None 表示不限量全量同步，按设计会推进账号断点
    report = await synchronizer.sync_accounts(limit=None)

    assert report.total_inserted == 2
    assert report.total_skipped == 1  # 3 解析 - 2 插入
    assert report.total_failed == 0
    assert report.results[0].inserted == 2
    # bulk_create 一次 + 断点推进一次 = 两次 execute
    assert session.execute.await_count == 2


async def test_sync_accounts_limit_mode_does_not_advance_checkpoint():
    account = _account(last_sync_uid=5)
    session = _mock_session([account])
    reader = FakeReader({1: _messages(6, 7, 8)})
    synchronizer = EmailSynchronizer(FakeDatabase(session), reader, _config())

    report = await synchronizer.sync_accounts(limit=10)

    assert report.total_inserted == 2
    # 限量模式不推进断点：仅 bulk_create 一次 execute
    assert session.execute.await_count == 1


async def test_sync_accounts_isolates_per_account_failure():
    accounts = [_account(1), _account(2)]
    session = _mock_session(accounts)
    reader = FakeReader({1: _messages(6), 2: RuntimeError("read boom")})
    synchronizer = EmailSynchronizer(FakeDatabase(session), reader, _config())

    report = await synchronizer.sync_accounts()

    assert report.total_failed == 1
    assert report.total_inserted == 2  # 仅账号 1 成功
    failed = [r for r in report.results if r.error is not None]
    assert len(failed) == 1
    assert "read boom" in failed[0].error


async def test_sync_accounts_empty_messages_does_not_write():
    account = _account()
    session = _mock_session([account])
    reader = FakeReader({1: []})
    synchronizer = EmailSynchronizer(FakeDatabase(session), reader, _config())

    report = await synchronizer.sync_accounts()

    assert report.total_inserted == 0
    assert report.total_failed == 0
    # 无邮件时不打开存储事务、不执行写入
    session.execute.assert_not_awaited()
