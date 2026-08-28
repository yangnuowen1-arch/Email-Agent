"""Port for read-only access to archived mail."""

from __future__ import annotations

from typing import Protocol

from app.schemas.mail_query import MailContext, MailSearchCriteria, MailSearchItem


class MailQueryStore(Protocol):
    """Retrieve mail projections while enforcing the trusted account scope."""

    async def search(
        self,
        criteria: MailSearchCriteria,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[MailSearchItem]:
        """Return only mail belonging to the supplied account IDs."""

    async def get_context(
        self,
        email_id: int,
        *,
        allowed_account_ids: frozenset[int],
    ) -> MailContext | None:
        """Return one accessible mail context, or ``None`` when it is unavailable."""
