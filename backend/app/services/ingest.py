"""Application service for inbound mail sync and archival."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.ports import EmailSyncStore, InboundMailbox
from app.schemas import (
    AccountSpec,
    AccountSyncResult,
    MailboxReadResult,
    ParsedEmail,
    PersistResult,
    RawEmail,
    SyncReport,
    SyncRequest,
)
from app.services.parsing import parse_email

logger = logging.getLogger(__name__)

EmailParser = Callable[[RawEmail], ParsedEmail]


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestPolicy:
    """Runtime limits for the inbound sync use case."""

    max_workers: int = 5
    timeout_seconds: float = 60

    def __post_init__(self) -> None:
        if not isinstance(self.max_workers, int) or self.max_workers < 1:
            msg = f"max_workers must be a positive int, got {self.max_workers!r}"
            raise ValueError(msg)
        if not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0:
            msg = f"timeout_seconds must be positive, got {self.timeout_seconds!r}"
            raise ValueError(msg)


class IngestCoordinator:
    """Coordinate read → parse → persist without knowing IMAP or SQLAlchemy.

    The service owns sync policy: UID start selection, account-level isolation,
    concurrency, timeout, and checkpoint safety.  Adapters own protocol and
    transaction details behind ``InboundMailbox`` and ``EmailSyncStore``.
    """

    def __init__(
        self,
        inbox: InboundMailbox,
        store: EmailSyncStore,
        policy: IngestPolicy,
        *,
        parser: EmailParser = parse_email,
    ) -> None:
        self._inbox = inbox
        self._store = store
        self._policy = policy
        self._parser = parser

    async def ingest(self, request: SyncRequest) -> SyncReport:
        """Synchronize all enabled accounts according to a typed request."""

        self._validate_request(request)
        start = time.monotonic()
        accounts = await self._store.list_enabled_accounts()
        semaphore = asyncio.Semaphore(self._policy.max_workers)
        results = await asyncio.gather(
            *(self._ingest_one(account, request, semaphore) for account in accounts)
        )
        ordered_results = tuple(sorted(results, key=lambda result: result.account_id))

        return SyncReport(
            results=ordered_results,
            total_inserted=sum(result.inserted for result in ordered_results),
            total_skipped=sum(result.skipped for result in ordered_results),
            total_failed=sum(result.error is not None for result in ordered_results),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def _ingest_one(
        self,
        account: AccountSpec,
        request: SyncRequest,
        semaphore: asyncio.Semaphore,
    ) -> AccountSyncResult:
        async with semaphore:
            try:
                return await self._sync_account(account, request)
            except Exception as exc:  # noqa: BLE001 - account failures must be isolated
                logger.error(
                    "inbound sync failed for account %s (id=%s): %s",
                    account.name,
                    account.account_id,
                    exc,
                )
                return AccountSyncResult(
                    account_id=account.account_id,
                    name=account.name,
                    error=str(exc),
                )

    async def _sync_account(
        self,
        account: AccountSpec,
        request: SyncRequest,
    ) -> AccountSyncResult:
        start = time.monotonic()
        after_uid = 0 if request.full else account.last_sync_uid
        read_result = await asyncio.wait_for(
            self._inbox.read(account, after_uid=after_uid, limit=request.limit),
            timeout=self._policy.timeout_seconds,
        )
        messages, failed_uids = self._parse_read_result(account, read_result)

        checkpoint_uid = self._checkpoint_for(account, request, messages, failed_uids)
        persisted = await self._persist(account, messages, checkpoint_uid)

        failed_uids_tuple = tuple(sorted(failed_uids))
        error = self._failure_message(failed_uids_tuple) if failed_uids_tuple else None
        return AccountSyncResult(
            account_id=account.account_id,
            name=account.name,
            inserted=persisted.inserted,
            skipped=persisted.skipped,
            failed_uids=failed_uids_tuple,
            checkpoint_advanced=(
                checkpoint_uid is not None and checkpoint_uid > account.last_sync_uid
            ),
            error=error,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _parse_read_result(
        self,
        account: AccountSpec,
        read_result: MailboxReadResult,
    ) -> tuple[list[ParsedEmail], set[int]]:
        raw_messages = tuple(read_result.messages)
        failed_uid_values = tuple(read_result.failed_uids)
        self._validate_read_result(raw_messages, failed_uid_values)

        messages: list[ParsedEmail] = []
        failed_uids = set(failed_uid_values)

        for raw_email in raw_messages:
            if raw_email.account_id != account.account_id:
                msg = "inbound adapter returned a message for a different account"
                raise ValueError(msg)
            try:
                message = self._parser(raw_email)
            except Exception as exc:  # noqa: BLE001 - retry policy is handled below
                logger.warning(
                    "mail parse failed for account %s uid %s: %s",
                    account.name,
                    raw_email.uid,
                    exc,
                )
                failed_uids.add(raw_email.uid)
                continue

            if message.account_id != account.account_id or message.uid != raw_email.uid:
                msg = "parser returned a message with mismatched account or UID"
                raise ValueError(msg)
            messages.append(message)

        return messages, failed_uids

    @staticmethod
    def _validate_request(request: SyncRequest) -> None:
        if not isinstance(request.full, bool):
            msg = f"full must be bool, got {request.full!r}"
            raise TypeError(msg)
        if request.limit is not None and (not isinstance(request.limit, int) or request.limit <= 0):
            msg = f"limit must be positive int or None, got {request.limit!r}"
            raise ValueError(msg)

    @staticmethod
    def _validate_read_result(
        messages: tuple[RawEmail, ...],
        failed_uids: tuple[int, ...],
    ) -> None:
        message_uids = [message.uid for message in messages]
        if len(message_uids) != len(set(message_uids)):
            raise ValueError("messages must not contain duplicate UIDs")
        if any(not isinstance(uid, int) or uid < 0 for uid in failed_uids):
            raise ValueError("failed_uids must contain non-negative integers")
        if set(message_uids).intersection(failed_uids):
            raise ValueError("a UID cannot be both returned and failed")

    @staticmethod
    def _checkpoint_for(
        account: AccountSpec,
        request: SyncRequest,
        messages: list[ParsedEmail],
        failed_uids: set[int],
    ) -> int | None:
        """Return the durable cursor only for a fully safe, unbounded run."""

        if request.limit is not None or failed_uids:
            return None
        highest_uid = max((message.uid for message in messages), default=account.last_sync_uid)
        # Full scans must never move a previously higher cursor backwards.
        return max(account.last_sync_uid, highest_uid)

    async def _persist(
        self,
        account: AccountSpec,
        messages: list[ParsedEmail],
        checkpoint_uid: int | None,
    ) -> PersistResult:
        if not messages and checkpoint_uid is None:
            return PersistResult()
        result = await self._store.persist(account, messages, checkpoint_uid=checkpoint_uid)
        if result.inserted < 0 or result.skipped < 0:
            raise ValueError("inserted and skipped must be non-negative")
        return result

    @staticmethod
    def _failure_message(failed_uids: tuple[int, ...]) -> str:
        sample = ", ".join(str(uid) for uid in failed_uids[:10])
        suffix = "" if len(failed_uids) <= 10 else ", …"
        return (
            f"{len(failed_uids)} message(s) could not be read or parsed "
            f"(UIDs: {sample}{suffix}); checkpoint was not advanced"
        )
