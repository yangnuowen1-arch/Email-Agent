from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.models.account import Account
from app.repository.email_accounts import AccountStore


@pytest.fixture
def session():
    s = MagicMock()
    s.scalars.return_value.all.return_value = [
        Account(id=1, name="a", host="h", username="u", password="p"),
    ]
    return s


def test_get_enabled_accounts_returns_list(session):
    store = AccountStore(session)
    result = store.get_enabled_accounts()
    assert len(result) == 1
    assert result[0].id == 1
    # 执行了查询，且语句过滤 enabled 并按下标排序
    session.scalars.assert_called_once()
    stmt = session.scalars.call_args[0][0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "email_accounts" in sql
    assert "enabled" in sql
    assert "order by" in sql.lower()


def test_get_enabled_accounts_empty_returns_empty(session):
    session.scalars.return_value.all.return_value = []
    store = AccountStore(session)
    assert store.get_enabled_accounts() == []


def test_get_enabled_accounts_wraps_db_error(session):
    session.scalars.side_effect = Exception("db down")
    store = AccountStore(session)
    with pytest.raises(RuntimeError, match="failed to fetch enabled accounts"):
        store.get_enabled_accounts()


def test_update_checkpoint_executes_update(session):
    store = AccountStore(session)
    now = datetime.now(UTC)
    store.update_checkpoint(account_id=10, last_sync_uid=42, last_sync_at=now)
    session.execute.assert_called_once()
    stmt = session.execute.call_args[0][0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "update email_accounts" in sql.lower()
    assert "last_sync_uid" in sql
    assert "last_sync_at" in sql


def test_update_checkpoint_uses_now_when_none(session):
    store = AccountStore(session)
    store.update_checkpoint(account_id=1, last_sync_uid=1, last_sync_at=None)
    stmt = session.execute.call_args[0][0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    # now() 由数据库/驱动填充，SQL 中出现 last_sync_at 的赋值
    assert "last_sync_at" in sql


def test_update_checkpoint_invalid_account_id_raises(session):
    store = AccountStore(session)
    with pytest.raises(ValueError, match="account_id"):
        store.update_checkpoint(account_id=0, last_sync_uid=1, last_sync_at=None)


def test_update_checkpoint_invalid_uid_raises(session):
    store = AccountStore(session)
    with pytest.raises(ValueError, match="last_sync_uid"):
        store.update_checkpoint(account_id=1, last_sync_uid=-1, last_sync_at=None)


def test_update_checkpoint_wraps_db_error(session):
    session.execute.side_effect = Exception("boom")
    store = AccountStore(session)
    with pytest.raises(RuntimeError, match="failed to update checkpoint"):
        store.update_checkpoint(account_id=1, last_sync_uid=1, last_sync_at=None)
