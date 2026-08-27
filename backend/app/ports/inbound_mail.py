"""Port for reading inbound mail without exposing a concrete protocol or SDK."""

from __future__ import annotations

from typing import Protocol

from app.schemas import AccountSpec, MailboxReadResult


class InboundMailbox(Protocol):
    """Read raw messages from one configured mailbox.

    The caller supplies the already-decided UID boundary.  This keeps checkpoint
    policy in the application service rather than in an IMAP adapter.
    """

    async def read(
        self,
        account: AccountSpec,
        *,
        after_uid: int,
        limit: int | None,
    ) -> MailboxReadResult:
        """Return available messages and explicitly report unavailable UIDs."""
