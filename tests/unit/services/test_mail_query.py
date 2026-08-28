"""Unit tests for the deterministic archived-mail query service."""

from __future__ import annotations

from app.schemas import MailContext, MailSearchCriteria, MailSearchItem
from app.services.mail_query import MailAccessDeniedError, MailQueryService


class FakeMailQueryStore:
    def __init__(self) -> None:
        self.search_calls: list[tuple[MailSearchCriteria, frozenset[int]]] = []
        self.context_calls: list[tuple[int, frozenset[int]]] = []
        self.items = [
            MailSearchItem(
                email_id=11,
                account_id=1,
                subject="Quote",
                sender="seller@example.com",
                sent_at=None,
                snippet="Please find the quote attached.",
                fetched_at=None,
            )
        ]
        self.context: MailContext | None = None

    async def search(
        self,
        criteria: MailSearchCriteria,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[MailSearchItem]:
        self.search_calls.append((criteria, allowed_account_ids))
        return self.items

    async def get_context(
        self,
        email_id: int,
        *,
        allowed_account_ids: frozenset[int],
    ) -> MailContext | None:
        self.context_calls.append((email_id, allowed_account_ids))
        return self.context


async def test_search_forwards_valid_criteria_and_scope() -> None:
    store = FakeMailQueryStore()
    service = MailQueryService(store)
    criteria = MailSearchCriteria(text="quote", account_id=1, limit=5)

    result = await service.search(criteria, allowed_account_ids=frozenset({1, 2}))

    assert result == store.items
    assert store.search_calls == [(criteria, frozenset({1, 2}))]


async def test_search_rejects_explicit_account_outside_scope() -> None:
    store = FakeMailQueryStore()
    service = MailQueryService(store)

    try:
        await service.search(
            MailSearchCriteria(text="quote", account_id=2),
            allowed_account_ids=frozenset({1}),
        )
    except MailAccessDeniedError:
        pass
    else:
        raise AssertionError("an inaccessible account must be rejected")

    assert store.search_calls == []


async def test_get_context_keeps_scope_when_message_is_missing() -> None:
    store = FakeMailQueryStore()
    service = MailQueryService(store)

    result = await service.get_context(88, allowed_account_ids=frozenset())

    assert result is None
    assert store.context_calls == [(88, frozenset())]
