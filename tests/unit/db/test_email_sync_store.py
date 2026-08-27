"""SQLAlchemy sync-store adapter tests with repository fakes."""

from __future__ import annotations

from contextlib import asynccontextmanager

from app.db.db import Account
from app.db.email_sync_store import SqlAlchemyEmailSyncStore
from app.schemas import AccountSpec, ParsedEmail


class FakeDatabase:
    def __init__(self) -> None:
        self.session_object = object()
        self.session_calls = 0

    @asynccontextmanager
    async def session(self):
        self.session_calls += 1
        yield self.session_object


def _account() -> Account:
    return Account(
        id=1,
        name="acc",
        host="imap.example.com",
        username="u@example.com",
        password="secret",
        last_sync_uid=5,
    )


async def test_list_enabled_accounts_projects_orm_models_to_pure_specs(monkeypatch) -> None:
    account = _account()

    class AccountRepository:
        def __init__(self, session) -> None:
            assert session is database.session_object

        async def list_account(self, *, enabled_only: bool):
            assert enabled_only is True
            return [account]

    database = FakeDatabase()
    monkeypatch.setattr("app.db.email_sync_store.EmailAccountRepository", AccountRepository)

    specs = await SqlAlchemyEmailSyncStore(database).list_enabled_accounts()

    assert len(specs) == 1
    assert specs[0].account_id == 1
    assert specs[0].last_sync_uid == 5
    assert specs[0].password == "secret"


async def test_persist_keeps_message_write_and_checkpoint_in_one_session(monkeypatch) -> None:
    captured_messages = []
    captured_checkpoints = []

    class AccountRepository:
        def __init__(self, session) -> None:
            assert session is database.session_object

        async def update_account_checkpoint(self, account_id: int, checkpoint_uid: int) -> None:
            captured_checkpoints.append((account_id, checkpoint_uid))

    class EmailRepository:
        def __init__(self, session) -> None:
            assert session is database.session_object

        async def bulk_create_email(self, messages) -> int:
            captured_messages.extend(messages)
            return 1

    database = FakeDatabase()
    monkeypatch.setattr("app.db.email_sync_store.EmailAccountRepository", AccountRepository)
    monkeypatch.setattr("app.db.email_sync_store.EmailRepository", EmailRepository)
    store = SqlAlchemyEmailSyncStore(database)
    # Use the pure account contract directly; the projection path is covered above.
    spec = AccountSpec(
        account_id=1,
        name="acc",
        last_sync_uid=5,
        host="imap.example.com",
        username="u@example.com",
        password="secret",
    )
    result = await store.persist(
        spec,
        [ParsedEmail(account_id=1, uid=6, subject="hello")],
        checkpoint_uid=6,
    )

    assert result.inserted == 1
    assert result.skipped == 0
    assert database.session_calls == 1
    assert [
        (message.account_id, message.uid, message.subject) for message in captured_messages
    ] == [(1, 6, "hello")]
    assert captured_checkpoints == [(1, 6)]


async def test_persist_does_not_touch_checkpoint_for_limit_or_partial_failure(monkeypatch) -> None:
    captured_checkpoints = []

    class AccountRepository:
        def __init__(self, session) -> None:
            pass

        async def update_account_checkpoint(self, account_id: int, checkpoint_uid: int) -> None:
            captured_checkpoints.append((account_id, checkpoint_uid))

    class EmailRepository:
        def __init__(self, session) -> None:
            pass

        async def bulk_create_email(self, messages) -> int:
            return len(messages)

    database = FakeDatabase()
    monkeypatch.setattr("app.db.email_sync_store.EmailAccountRepository", AccountRepository)
    monkeypatch.setattr("app.db.email_sync_store.EmailRepository", EmailRepository)
    spec = AccountSpec(
        account_id=1,
        name="acc",
        last_sync_uid=5,
        host="imap.example.com",
        username="u@example.com",
        password="secret",
    )

    await SqlAlchemyEmailSyncStore(database).persist(
        spec,
        [ParsedEmail(account_id=1, uid=6)],
        checkpoint_uid=None,
    )

    assert captured_checkpoints == []
