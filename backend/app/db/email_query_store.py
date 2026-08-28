"""SQLAlchemy adapter for the read-only archived-mail query port."""

from __future__ import annotations

from app.db.db import EmailMessage
from app.db.engine import Database
from app.db.repositories import EmailRepository
from app.schemas.mail_query import MailContext, MailSearchCriteria, MailSearchItem


def _email_id(message: EmailMessage) -> int:
    """Require the primary key that exists on a message read from storage."""

    if message.id is None:
        raise ValueError("stored email is missing its primary key")
    return message.id


def _snippet(body: str | None, *, max_chars: int = 280) -> str | None:
    """Collapse whitespace so search output stays small and model-consumable."""

    if body is None:
        return None
    compact = " ".join(body.split())
    if not compact:
        return None
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1]}…"


def _to_search_item(message: EmailMessage) -> MailSearchItem:
    return MailSearchItem(
        email_id=_email_id(message),
        account_id=message.account_id,
        subject=message.subject,
        sender=message.sender,
        sent_at=message.sent_at,
        snippet=_snippet(message.text_body),
        fetched_at=message.fetched_at,
    )


def _to_context(message: EmailMessage) -> MailContext:
    return MailContext(
        email_id=_email_id(message),
        account_id=message.account_id,
        uid=message.uid,
        message_id=message.message_id,
        subject=message.subject,
        sender=message.sender,
        recipients=tuple(message.recipients or ()),
        sent_at=message.sent_at,
        text_body=message.text_body,
        fetched_at=message.fetched_at,
    )


class SqlAlchemyMailQueryStore:
    """Map SQLAlchemy rows to pure query contracts inside a transaction boundary."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def search(
        self,
        criteria: MailSearchCriteria,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[MailSearchItem]:
        async with self._database.session() as session:
            messages = await EmailRepository(session).search_emails(
                criteria,
                allowed_account_ids=allowed_account_ids,
            )
        return [_to_search_item(message) for message in messages]

    async def get_context(
        self,
        email_id: int,
        *,
        allowed_account_ids: frozenset[int],
    ) -> MailContext | None:
        async with self._database.session() as session:
            message = await EmailRepository(session).get_email_by_id_in_accounts(
                email_id,
                allowed_account_ids=allowed_account_ids,
            )
        return _to_context(message) if message is not None else None
