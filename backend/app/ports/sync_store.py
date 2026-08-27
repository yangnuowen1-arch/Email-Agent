"""Port for the persistence operations required by inbound mail sync."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.schemas import AccountSpec, ParsedEmail, PersistResult


class EmailSyncStore(Protocol):
    """Store mailbox sync state without exposing ORM or transaction details."""

    async def list_enabled_accounts(self) -> list[AccountSpec]:
        """Return the accounts eligible for this sync run."""

    async def persist(
        self,
        account: AccountSpec,
        messages: Sequence[ParsedEmail],
        *,
        checkpoint_uid: int | None,
    ) -> PersistResult:
        """Atomically persist mail and, when supplied, update the checkpoint.

        A non-``None`` checkpoint is also a successful-sync heartbeat, even if
        it equals the existing UID.  The concrete adapter therefore refreshes
        ``last_sync_at`` in the same transaction.
        """
