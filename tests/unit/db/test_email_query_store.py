"""Tests for the SQLAlchemy archived-mail query adapter."""

from __future__ import annotations

from contextlib import asynccontextmanager

from app.db.db import EmailMessage
from app.db.email_query_store import SqlAlchemyMailQueryStore
from app.schemas import MailSearchCriteria


class FakeDatabase:
    def __init__(self) -> None:
        self.session_object = object()
        self.session_calls = 0

    @asynccontextmanager
    async def session(self):
        self.session_calls += 1
        yield self.session_object


async def test_search_projects_orm_messages_and_preserves_scope(monkeypatch) -> None:
    message = EmailMessage(
        id=9,
        account_id=1,
        uid=44,
        subject="Quote",
        sender="seller@example.com",
        text_body="  Hello\n  here is the quote.  ",
    )
    captured: list[tuple[MailSearchCriteria, frozenset[int]]] = []

    class Repository:
        def __init__(self, session) -> None:
            assert session is database.session_object

        async def search_emails(
            self,
            criteria: MailSearchCriteria,
            *,
            allowed_account_ids: frozenset[int],
        ) -> list[EmailMessage]:
            captured.append((criteria, allowed_account_ids))
            return [message]

    database = FakeDatabase()
    monkeypatch.setattr("app.db.email_query_store.EmailRepository", Repository)
    criteria = MailSearchCriteria(text="quote", limit=3)

    result = await SqlAlchemyMailQueryStore(database).search(
        criteria,
        allowed_account_ids=frozenset({1}),
    )

    assert database.session_calls == 1
    assert captured == [(criteria, frozenset({1}))]
    assert result[0].email_id == 9
    assert result[0].snippet == "Hello here is the quote."


async def test_get_context_returns_none_for_no_accessible_message(monkeypatch) -> None:
    captured: list[tuple[int, frozenset[int]]] = []

    class Repository:
        def __init__(self, session) -> None:
            assert session is database.session_object

        async def get_email_by_id_in_accounts(
            self,
            email_id: int,
            *,
            allowed_account_ids: frozenset[int],
        ) -> None:
            captured.append((email_id, allowed_account_ids))
            return None

    database = FakeDatabase()
    monkeypatch.setattr("app.db.email_query_store.EmailRepository", Repository)

    result = await SqlAlchemyMailQueryStore(database).get_context(
        9,
        allowed_account_ids=frozenset({1}),
    )

    assert result is None
    assert captured == [(9, frozenset({1}))]
