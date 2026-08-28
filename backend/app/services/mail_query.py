"""Deterministic, read-only use cases over archived mail."""

from __future__ import annotations

from app.ports.mail_query import MailQueryStore
from app.schemas.mail_query import MailContext, MailSearchCriteria, MailSearchItem


class MailAccessDeniedError(PermissionError):
    """Raised when a caller explicitly requests an account outside its scope."""


class MailQueryService:
    """Apply query rules before delegating to a replaceable read-only store."""

    def __init__(self, store: MailQueryStore) -> None:
        self._store = store

    async def search(
        self,
        criteria: MailSearchCriteria,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[MailSearchItem]:
        """Search only within the caller's allowed mailbox set."""

        scope = self._validate_account_scope(allowed_account_ids)
        self._validate_criteria(criteria)
        if criteria.account_id is not None and criteria.account_id not in scope:
            raise MailAccessDeniedError("requested account is outside the caller scope")
        return await self._store.search(criteria, allowed_account_ids=scope)

    async def get_context(
        self,
        email_id: int,
        *,
        allowed_account_ids: frozenset[int],
    ) -> MailContext | None:
        """Retrieve one accessible message without revealing other-account ownership."""

        if type(email_id) is not int or email_id <= 0:
            raise ValueError("email_id must be a positive int")
        scope = self._validate_account_scope(allowed_account_ids)
        return await self._store.get_context(email_id, allowed_account_ids=scope)

    @staticmethod
    def _validate_account_scope(account_ids: frozenset[int]) -> frozenset[int]:
        if any(type(account_id) is not int or account_id <= 0 for account_id in account_ids):
            raise ValueError("allowed_account_ids must contain positive integers")
        return account_ids

    @staticmethod
    def _validate_criteria(criteria: MailSearchCriteria) -> None:
        if not criteria.text and not criteria.sender:
            raise ValueError("mail search requires text or sender")
        if criteria.account_id is not None and (
            type(criteria.account_id) is not int or criteria.account_id <= 0
        ):
            raise ValueError("account_id must be a positive int or None")
        if type(criteria.limit) is not int or not 1 <= criteria.limit <= 50:
            raise ValueError("limit must be an int in 1..50")
