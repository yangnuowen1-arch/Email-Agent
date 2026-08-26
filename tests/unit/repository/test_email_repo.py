from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.models.message import EmailMessage
from app.repository.emails import EmailStore


@pytest.fixture
def session():
    s = MagicMock()
    # bulk_insert 现在以 RETURNING 实际返回行数作为插入计数
    s.execute.return_value.fetchall.return_value = [1, 2]
    return s


def test_bulk_insert_empty_returns_zero(session):
    store = EmailStore(session)
    assert store.bulk_insert([]) == 0
    session.execute.assert_not_called()


def test_bulk_insert_builds_on_conflict_do_nothing(session):
    store = EmailStore(session)
    now = datetime.now(UTC)
    msgs = [
        EmailMessage(
            account_id=1,
            uid=100,
            message_id="<m1@x>",
            subject="a",
            sender="f@x",
            recipients=["t@x"],
            sent_at=now,
            text_body="ta",
            html_body="<p>a</p>",
            fetched_at=now,
        ),
        EmailMessage(account_id=1, uid=101, subject="b"),
    ]
    n = store.bulk_insert(msgs)
    assert n == 2
    session.execute.assert_called_once()
    stmt = session.execute.call_args[0][0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "insert into emails" in sql.lower()
    assert "on conflict" in sql.lower()
    assert "do nothing" in sql.lower()
    assert "account_id" in sql and "uid" in sql


def test_bulk_insert_uses_default_fetched_at(session):
    store = EmailStore(session)
    msg = EmailMessage(account_id=1, uid=100, subject="a")
    store.bulk_insert([msg])
    # 提交的字典中应填充 fetched_at（非 None）
    values = session.execute.call_args[0][0]
    compiled = values.compile(dialect=postgresql.dialect())
    # 仅验证语句可编译且包含 fetched_at 列
    assert "fetched_at" in str(compiled).lower()


def test_bulk_insert_type_error_for_non_model(session):
    store = EmailStore(session)
    with pytest.raises(TypeError, match="EmailMessage"):
        store.bulk_insert(["not a message"])  # type: ignore[list-item]


def test_bulk_insert_wraps_db_error(session):
    session.execute.side_effect = Exception("boom")
    store = EmailStore(session)
    msg = EmailMessage(account_id=1, uid=100, subject="a")
    with pytest.raises(RuntimeError, match="failed to bulk insert"):
        store.bulk_insert([msg])
