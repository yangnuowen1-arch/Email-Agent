from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.models.account import Account
from app.providers.email.base import MailClient, MailClientError
from app.schemas.email import ParsedEmail
from app.services.sync import sync_account, sync_all


def _account(**overrides) -> Account:
    base = dict(
        id=1,
        name="qq",
        host="imap.qq.com",
        username="u@qq.com",
        password="secret",
        last_sync_uid=10,
    )
    base.update(overrides)
    return Account(**base)  # type: ignore[arg-type]


def _mock_email_store(**kwargs):
    store = MagicMock()
    store.bulk_insert = MagicMock(**kwargs.get("bulk_insert_kwargs", {}))
    if "bulk_insert_return" in kwargs:
        store.bulk_insert.return_value = kwargs["bulk_insert_return"]
    return store


def _mock_checkpoint_store():
    store = MagicMock()
    store.update_checkpoint = MagicMock()
    return store


def _mock_session():
    # Session 是事务单元：commit/rollback 由 sync_account 统一调用
    session = MagicMock()
    # 真实 EmailStore/AccountStore 包裹该 session 时，execute 返回带 rowcount 的结果
    session.execute.return_value.rowcount = 1
    return session


class FakeMailClient(MailClient):
    def __init__(self, account, uids_to_return=None, should_fail=False):
        super().__init__(account)
        self.uids_to_return = uids_to_return or []
        self.should_fail = should_fail
        self.connect_called = False
        self.close_called = False

    def connect(self) -> None:
        self.connect_called = True
        if self.should_fail:
            raise MailClientError(f"[{self.account.name}] fake connect fail")

    def fetch_emails(self, folder, since_uid, limit=None):
        if self.should_fail:
            raise MailClientError("fetch fail")
        # simulate UID filtering: return sorted uids > since_uid, limited
        filtered = [uid for uid in self.uids_to_return if uid > since_uid]
        filtered = sorted(filtered)
        if limit is not None:
            filtered = filtered[:limit]
        return [(uid, f"raw-{uid}".encode()) for uid in filtered]

    def close(self) -> None:
        self.close_called = True


def test_sync_account_full_flow_inserts_and_updates_state():
    acc = _account(id=1, name="qq", last_sync_uid=10)
    email_store = _mock_email_store(bulk_insert_return=2)
    checkpoint_store = _mock_checkpoint_store()
    session = _mock_session()

    def factory(a):
        return FakeMailClient(a, uids_to_return=[11, 12])

    def parser(raw, account_id, uid):
        return ParsedEmail(account_id=account_id, uid=uid, subject=f"s{uid}")

    result = sync_account(
        acc,
        session=session,
        email_store=email_store,
        checkpoint_store=checkpoint_store,
        client_factory=factory,
        parser=parser,
        full=False,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert result.fetched == 2
    assert result.parsed == 2
    assert result.inserted == 2
    assert result.skipped == 0
    assert result.max_uid == 12
    assert result.error is None
    email_store.bulk_insert.assert_called_once()
    # bulk_insert called with 2 messages
    assert len(email_store.bulk_insert.call_args[0][0]) == 2
    checkpoint_store.update_checkpoint.assert_called_once_with(
        1, 12, datetime(2026, 8, 25, tzinfo=UTC)
    )
    # 跨表写操作由一次 session.commit() 原子提交
    session.commit.assert_called_once()
    session.rollback.assert_not_called()


def test_sync_account_full_flag_ignores_breakpoint():
    acc = _account(id=1, last_sync_uid=999)
    captured = {}

    class CapturingClient(FakeMailClient):
        def fetch_emails(self, folder, since_uid, limit=None):
            captured["since_uid"] = since_uid
            return super().fetch_emails(folder, since_uid, limit)

    def factory(a):
        return CapturingClient(a, uids_to_return=[1, 2])

    email_store = _mock_email_store(bulk_insert_return=2)
    checkpoint_store = _mock_checkpoint_store()
    session = _mock_session()

    result = sync_account(
        acc,
        session=session,
        email_store=email_store,
        checkpoint_store=checkpoint_store,
        client_factory=factory,
        full=True,
    )

    assert captured["since_uid"] == 0
    assert result.fetched == 2


def test_sync_account_limit_does_not_update_state():
    acc = _account(id=1, last_sync_uid=0)
    email_store = _mock_email_store(bulk_insert_return=1)
    checkpoint_store = _mock_checkpoint_store()
    session = _mock_session()

    def factory(a):
        return FakeMailClient(a, uids_to_return=[1, 2, 3])

    result = sync_account(
        acc,
        session=session,
        email_store=email_store,
        checkpoint_store=checkpoint_store,
        client_factory=factory,
        limit=1,
        full=False,
    )

    # limit=1 should fetch only 1, and NOT update breakpoint
    assert result.fetched == 1
    assert result.max_uid == 1
    checkpoint_store.update_checkpoint.assert_not_called()
    # 邮件仍需落库：一次提交
    session.commit.assert_called_once()
    session.rollback.assert_not_called()


def test_sync_account_empty_mailbox_no_update():
    acc = _account(last_sync_uid=5)
    email_store = _mock_email_store()
    checkpoint_store = _mock_checkpoint_store()
    session = _mock_session()

    def factory(a):
        return FakeMailClient(a, uids_to_return=[])

    result = sync_account(
        acc,
        session=session,
        email_store=email_store,
        checkpoint_store=checkpoint_store,
        client_factory=factory,
    )

    assert result.fetched == 0
    assert result.inserted == 0
    email_store.bulk_insert.assert_not_called()
    checkpoint_store.update_checkpoint.assert_not_called()
    assert result.error is None
    session.rollback.assert_called()
    session.commit.assert_not_called()


def test_sync_account_bulk_insert_conflict_skips():
    acc = _account(last_sync_uid=0)
    email_store = _mock_email_store(bulk_insert_return=1)
    checkpoint_store = _mock_checkpoint_store()
    session = _mock_session()

    def factory(a):
        return FakeMailClient(a, uids_to_return=[1, 2])

    def parser(raw, account_id, uid):
        return ParsedEmail(account_id=account_id, uid=uid)

    result = sync_account(
        acc,
        session=session,
        email_store=email_store,
        checkpoint_store=checkpoint_store,
        client_factory=factory,
        parser=parser,
    )

    assert result.parsed == 2
    assert result.inserted == 1
    assert result.skipped == 1
    checkpoint_store.update_checkpoint.assert_called_once()  # limit is None, so update


def test_sync_account_client_error_returns_error_result():
    acc = _account(id=1, name="bad", password="secret123")
    email_store = _mock_email_store()
    checkpoint_store = _mock_checkpoint_store()
    session = _mock_session()

    def factory(a):
        return FakeMailClient(a, should_fail=True)

    result = sync_account(
        acc,
        session=session,
        email_store=email_store,
        checkpoint_store=checkpoint_store,
        client_factory=factory,
    )

    assert result.error is not None
    assert "bad" in result.error
    assert "secret123" not in result.error
    session.rollback.assert_called()
    session.commit.assert_not_called()


def test_sync_account_parser_exception_skipped():
    acc = _account(last_sync_uid=0)
    email_store = _mock_email_store(bulk_insert_return=1)
    checkpoint_store = _mock_checkpoint_store()
    session = _mock_session()

    def factory(a):
        return FakeMailClient(a, uids_to_return=[1, 2])

    def parser(raw, account_id, uid):
        if uid == 1:
            raise ValueError("bad raw")
        return ParsedEmail(account_id=account_id, uid=uid)

    result = sync_account(
        acc,
        session=session,
        email_store=email_store,
        checkpoint_store=checkpoint_store,
        client_factory=factory,
        parser=parser,
    )

    # one failed, one succeed
    assert result.fetched == 2
    assert result.parsed == 1
    assert result.inserted == 1


def test_sync_account_atomic_rollback_on_checkpoint_failure():
    # 验证多表原子性：bulk_insert 成功后若断点更新抛异常，整个事务回滚
    acc = _account(last_sync_uid=0)
    email_store = _mock_email_store(bulk_insert_return=2)
    checkpoint_store = _mock_checkpoint_store()
    checkpoint_store.update_checkpoint.side_effect = RuntimeError("checkpoint failed")
    session = _mock_session()

    def factory(a):
        return FakeMailClient(a, uids_to_return=[1, 2])

    def parser(raw, account_id, uid):
        return ParsedEmail(account_id=account_id, uid=uid)

    result = sync_account(
        acc,
        session=session,
        email_store=email_store,
        checkpoint_store=checkpoint_store,
        client_factory=factory,
        parser=parser,
    )

    # 邮件插入已发生，但断点失败 → 整体回滚，不应提交
    email_store.bulk_insert.assert_called_once()
    checkpoint_store.update_checkpoint.assert_called_once()
    session.rollback.assert_called()
    session.commit.assert_not_called()
    assert result.error is not None
    assert "checkpoint failed" in result.error


def test_sync_all_concurrent_isolation_one_fails():
    acc1 = _account(id=1, name="a1", last_sync_uid=0)
    acc2 = _account(id=2, name="a2", last_sync_uid=0)
    acc3 = _account(id=3, name="a3", last_sync_uid=0)

    def factory(a):
        if a.name == "a2":
            return FakeMailClient(a, should_fail=True)
        return FakeMailClient(a, uids_to_return=[1])

    results = sync_all(
        [acc1, acc2, acc3],
        session_factory=_mock_session,
        max_workers=2,
        timeout=5,
        client_factory=factory,
    )

    assert len(results) == 3
    results_by_id = {r.account_id: r for r in results}
    assert results_by_id[1].error is None
    assert results_by_id[2].error is not None
    assert results_by_id[3].error is None
    # ensure sorted by account_id
    assert [r.account_id for r in results] == [1, 2, 3]


def test_sync_all_timeout_marks_error():
    acc = _account(id=1, name="slow", last_sync_uid=0)

    def slow_factory(a):
        class SlowClient(MailClient):
            def __init__(self, account):
                super().__init__(account)

            def connect(self) -> None:
                import time

                time.sleep(0.2)

            def fetch_emails(self, folder, since_uid, limit=None):
                return []

            def close(self) -> None:
                pass

        return SlowClient(a)

    results = sync_all(
        [acc],
        session_factory=_mock_session,
        max_workers=1,
        timeout=0.05,
        client_factory=slow_factory,
    )

    assert len(results) == 1
    assert results[0].error is not None
    assert "timeout" in results[0].error.lower()


def test_sync_all_respects_max_workers():
    accs = [_account(id=i, name=f"a{i}") for i in range(1, 4)]

    def factory(a):
        return FakeMailClient(a, uids_to_return=[1])

    with patch("app.services.sync.ThreadPoolExecutor") as MockExecutor:
        mock_exec = MagicMock()
        MockExecutor.return_value.__enter__.return_value = mock_exec

        # Simulate submit returning futures that complete
        def fake_submit(fn, *args, **kwargs):
            fut = MagicMock()
            try:
                res = fn(*args, **kwargs)
                fut.result = MagicMock(return_value=res)
            except Exception as exc:
                fut.result = MagicMock(side_effect=exc)
            return fut

        mock_exec.submit.side_effect = fake_submit
        mock_exec.__exit__ = MagicMock(return_value=False)

        sync_all(
            accs,
            session_factory=_mock_session,
            max_workers=2,
            timeout=5,
            client_factory=factory,
        )

        MockExecutor.assert_called_once_with(max_workers=2)


def test_sync_all_empty_returns_empty():
    results = sync_all(
        [],
        session_factory=_mock_session,
        max_workers=2,
        timeout=5,
    )
    assert results == []


def test_sync_all_invalid_max_workers_raises():
    with pytest.raises(ValueError, match="max_workers"):
        sync_all(
            [_account()],
            session_factory=_mock_session,
            max_workers=0,
            timeout=5,
        )


def test_sync_account_invalid_limit_raises():
    acc = _account()
    email_store = _mock_email_store()
    checkpoint_store = _mock_checkpoint_store()
    session = _mock_session()
    with pytest.raises(ValueError, match="limit"):
        sync_account(
            acc,
            session=session,
            email_store=email_store,
            checkpoint_store=checkpoint_store,
            limit=0,
        )
    with pytest.raises(ValueError, match="limit"):
        sync_account(
            acc,
            session=session,
            email_store=email_store,
            checkpoint_store=checkpoint_store,
            limit=-1,
        )


def test_sync_account_ensure_close_called_even_on_error():
    acc = _account()
    email_store = _mock_email_store()
    checkpoint_store = _mock_checkpoint_store()
    session = _mock_session()

    client = FakeMailClient(acc, should_fail=True)

    # need factory returning same instance to check close_called
    def factory(a):
        return client

    result = sync_account(
        acc,
        session=session,
        email_store=email_store,
        checkpoint_store=checkpoint_store,
        client_factory=factory,
    )
    assert client.close_called is True
    assert result.error is not None
