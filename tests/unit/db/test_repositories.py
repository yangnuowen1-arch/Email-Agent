"""db.repositories 单元测试：异步仓储的行为与命名规范。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.db import Account, Base, EmailMessage
from app.db.repositories import EmailAccountRepository, EmailRepository
from app.schemas.mail_query import MailSearchCriteria


@pytest.fixture
async def sqlite_engine():
    """内存 SQLite：仅创建 email_accounts 表（emails 含 PG 方言 ARRAY 列，SQLite 不支持）。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Account.__table__]))
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(sqlite_engine):
    maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


def _make_account(**overrides):
    fields = {
        "name": "acc",
        "host": "imap.example.com",
        "username": "acc@example.com",
        "password": "secret",
    }
    return Account(**{**fields, **overrides})


class TestEmailAccountRepository:
    async def test_create_account_persists_and_returns_with_id(self, session):
        repo = EmailAccountRepository(session)
        acc = await repo.create_account(_make_account())

        assert acc.id is not None
        assert acc.name == "acc"

    async def test_get_account_by_id_found(self, session):
        repo = EmailAccountRepository(session)
        acc = await repo.create_account(_make_account(name="found"))

        found = await repo.get_account_by_id(acc.id)
        assert found is not None
        assert found.id == acc.id
        assert found.name == "found"

    async def test_get_account_by_id_not_found(self, session):
        repo = EmailAccountRepository(session)
        assert await repo.get_account_by_id(99999) is None

    async def test_list_account_returns_all(self, session):
        repo = EmailAccountRepository(session)
        await repo.create_account(_make_account(name="a"))
        await repo.create_account(_make_account(name="b"))

        accounts = await repo.list_account()
        assert len(accounts) == 2
        assert {a.name for a in accounts} == {"a", "b"}

    async def test_list_account_enabled_only(self, session):
        repo = EmailAccountRepository(session)
        await repo.create_account(_make_account(name="on", enabled=True))
        await repo.create_account(_make_account(name="off", enabled=False))

        enabled = await repo.list_account(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0].name == "on"

    async def test_update_account_checkpoint(self, session):
        repo = EmailAccountRepository(session)
        acc = await repo.create_account(_make_account(last_sync_uid=0))

        await repo.update_account_checkpoint(acc.id, 42)
        await session.flush()
        await session.refresh(acc)

        assert acc.last_sync_uid == 42
        assert acc.last_sync_at is not None

    async def test_update_account_checkpoint_never_moves_cursor_backwards(self, session):
        repo = EmailAccountRepository(session)
        acc = await repo.create_account(_make_account(last_sync_uid=42))

        await repo.update_account_checkpoint(acc.id, 5)
        await session.flush()
        await session.refresh(acc)

        assert acc.last_sync_uid == 42

    async def test_update_account_checkpoint_rejects_bad_id(self, session):
        repo = EmailAccountRepository(session)
        with pytest.raises(ValueError, match="account_id"):
            await repo.update_account_checkpoint(0, 1)

    async def test_delete_account_by_id(self, session):
        repo = EmailAccountRepository(session)
        acc = await repo.create_account(_make_account())
        aid = acc.id

        assert await repo.delete_account_by_id(aid) is True
        assert await repo.get_account_by_id(aid) is None

    async def test_delete_account_by_id_not_found(self, session):
        repo = EmailAccountRepository(session)
        assert await repo.delete_account_by_id(99999) is False

    async def test_create_account_rejects_non_account(self, session):
        repo = EmailAccountRepository(session)
        with pytest.raises(TypeError, match="expected Account"):
            await repo.create_account(MagicMock())


class TestEmailRepository:
    """使用 AsyncMock 会话测试 EmailRepository 逻辑（emails 表含 PG ARRAY 列，不在 SQLite 上建表）。"""

    def _mock_session(self):
        return AsyncMock(spec=AsyncSession)

    async def test_get_email_by_id_delegates_to_session_get(self):
        repo = EmailRepository(self._mock_session())
        await repo.get_email_by_id(5)

        repo.session.get.assert_awaited_once_with(EmailMessage, 5)

    async def test_get_email_composite_key(self):
        mock_session = self._mock_session()
        mock_session.scalar = AsyncMock(
            return_value=EmailMessage(account_id=1, uid=100, subject="hi")
        )

        repo = EmailRepository(mock_session)
        found = await repo.get_email(account_id=1, uid=100)

        assert found is not None
        assert found.uid == 100
        mock_session.scalar.assert_awaited_once()

    async def test_list_email_by_account_id(self):
        mock_session = self._mock_session()
        mock_session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))

        repo = EmailRepository(mock_session)
        result = await repo.list_email_by_account_id(1)

        assert result == []
        mock_session.scalars.assert_awaited_once()

    async def test_search_emails_scopes_the_database_query_to_allowed_accounts(self):
        mock_session = self._mock_session()
        mock_session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        repo = EmailRepository(mock_session)

        await repo.search_emails(
            MailSearchCriteria(text="quote", limit=5),
            allowed_account_ids=frozenset({1, 2}),
        )

        stmt = mock_session.scalars.call_args.args[0]
        sql = str(stmt.compile(dialect=sqlalchemy.dialects.postgresql.dialect()))
        assert "emails.account_id IN" in sql
        assert "ILIKE" in sql

    async def test_get_email_by_id_in_accounts_returns_none_without_scope(self):
        mock_session = self._mock_session()
        repo = EmailRepository(mock_session)

        result = await repo.get_email_by_id_in_accounts(9, allowed_account_ids=frozenset())

        assert result is None
        mock_session.scalar.assert_not_awaited()

    async def test_create_email_adds_and_flushes(self):
        mock_session = self._mock_session()
        mock_session.flush = AsyncMock()

        repo = EmailRepository(mock_session)
        msg = EmailMessage(account_id=1, uid=1, subject="x")
        saved = await repo.create_email(msg)

        mock_session.add.assert_called_once_with(msg)
        mock_session.flush.assert_awaited_once()
        assert saved is msg

    async def test_bulk_create_email_empty_returns_zero(self):
        repo = EmailRepository(self._mock_session())
        assert await repo.bulk_create_email([]) == 0

    async def test_bulk_create_email_rejects_non_model(self):
        repo = EmailRepository(self._mock_session())
        with pytest.raises(TypeError, match="expected EmailMessage"):
            await repo.bulk_create_email([MagicMock()])

    async def test_delete_email_by_id(self):
        mock_session = self._mock_session()
        mock_session.execute = AsyncMock(return_value=MagicMock(rowcount=1))

        repo = EmailRepository(mock_session)
        assert await repo.delete_email_by_id(5) is True
        mock_session.execute.assert_awaited_once()

    async def test_bulk_builds_pg_insert_statement(self):
        """验证 bulk_create_email 组装 PG 方言的 ON CONFLICT DO NOTHING 语句。"""
        mock_session = self._mock_session()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(fetchall=MagicMock(return_value=[1, 2]))
        )

        repo = EmailRepository(mock_session)
        msgs = [
            EmailMessage(account_id=1, uid=1, subject="a"),
            EmailMessage(account_id=1, uid=2, subject="b"),
        ]
        count = await repo.bulk_create_email(msgs)

        assert count == 2
        mock_session.execute.assert_awaited_once()
        stmt = mock_session.execute.call_args.args[0]
        compiled = stmt.compile(dialect=sqlalchemy.dialects.postgresql.dialect())
        sql = str(compiled)
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql
