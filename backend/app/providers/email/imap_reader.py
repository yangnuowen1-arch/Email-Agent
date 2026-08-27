"""IMAP-backed implementation of the inbound-mail application port."""

from __future__ import annotations

import asyncio
import contextlib

from app.ports.inbound_mail import InboundMailbox
from app.schemas import AccountSpec, MailboxReadResult, RawEmail

from .base import MailClientFactory, MailFetchResult

__all__ = ["ImapMailboxReader"]


def _blocking_read(
    account: AccountSpec,
    client_factory: MailClientFactory,
    after_uid: int,
    limit: int | None,
) -> MailFetchResult:
    """Run the blocking protocol sequence in a worker thread.

    A client exists only in the worker that uses it, avoiding cross-thread IMAP
    connection reuse. Closing remains best-effort so it never hides a fetch
    error from the caller.
    """

    client = client_factory(account)
    try:
        client.connect()
        return client.fetch_emails(account.folder, after_uid, limit=limit)
    finally:
        with contextlib.suppress(Exception):
            client.close()


class ImapMailboxReader(InboundMailbox):
    """Read RFC822 messages through a supplied IMAP client factory."""

    def __init__(self, client_factory: MailClientFactory) -> None:
        self._client_factory = client_factory

    async def read(
        self,
        account: AccountSpec,
        *,
        after_uid: int,
        limit: int | None,
    ) -> MailboxReadResult:
        if not isinstance(after_uid, int) or after_uid < 0:
            msg = f"after_uid must be int >=0, got {after_uid!r}"
            raise ValueError(msg)
        if limit is not None and (not isinstance(limit, int) or limit <= 0):
            msg = f"limit must be positive int or None, got {limit!r}"
            raise ValueError(msg)

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            _blocking_read,
            account,
            self._client_factory,
            after_uid,
            limit,
        )
        return MailboxReadResult(
            messages=tuple(
                RawEmail(account_id=account.account_id, uid=uid, raw=raw)
                for uid, raw in result.messages
            ),
            failed_uids=result.failed_uids,
        )
