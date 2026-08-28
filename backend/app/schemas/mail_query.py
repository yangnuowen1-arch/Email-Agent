"""Pure contracts for querying already archived mail.

These records deliberately contain only data needed by read-only use cases.
They do not expose ORM entities or any provider-specific type.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class MailSearchCriteria:
    """Filters for searching mail that has already been persisted."""

    text: str | None = None
    sender: str | None = None
    account_id: int | None = None
    limit: int = 10


@dataclass(frozen=True, slots=True, kw_only=True)
class MailSearchItem:
    """Small, model-safe mail projection returned by a search."""

    email_id: int
    account_id: int
    subject: str
    sender: str | None
    sent_at: datetime | None
    snippet: str | None
    fetched_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MailContext:
    """Plain-text context for one stored mail message."""

    email_id: int
    account_id: int
    uid: int
    message_id: str | None
    subject: str
    sender: str | None
    recipients: tuple[str, ...]
    sent_at: datetime | None
    text_body: str | None
    fetched_at: datetime | None
