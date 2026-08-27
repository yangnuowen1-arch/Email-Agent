"""Application-level tests for inbound sync, using only fake ports."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.schemas import (
    AccountSpec,
    MailboxReadResult,
    ParsedEmail,
    PersistResult,
    RawEmail,
    SyncRequest,
)
from app.services.ingest import IngestCoordinator, IngestPolicy


def _account(account_id: int = 1, last_sync_uid: int = 5) -> AccountSpec:
    return AccountSpec(
        account_id=account_id,
        name=f"acc{account_id}",
        last_sync_uid=last_sync_uid,
        host="imap.example.com",
        username="u@example.com",
        password="secret",
    )


def _read_result(
    account_id: int,
    *uids: int,
    failed_uids: tuple[int, ...] = (),
) -> MailboxReadResult:
    return MailboxReadResult(
        messages=tuple(
            RawEmail(account_id=account_id, uid=uid, raw=f"Subject: {uid}\r\n\r\nbody".encode())
            for uid in uids
        ),
        failed_uids=failed_uids,
    )


@dataclass
class FakeMailbox:
    per_account: dict[int, MailboxReadResult | Exception]
    calls: list[tuple[int, int, int | None]] = field(default_factory=list)

    async def read(self, account, *, after_uid: int, limit: int | None) -> MailboxReadResult:
        self.calls.append((account.account_id, after_uid, limit))
        result = self.per_account[account.account_id]
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeStore:
    accounts: list[AccountSpec]
    outcomes: dict[int, PersistResult] = field(default_factory=dict)
    persist_calls: list[tuple[int, list[ParsedEmail], int | None]] = field(default_factory=list)

    async def list_enabled_accounts(self) -> list[AccountSpec]:
        return self.accounts

    async def persist(
        self,
        account: AccountSpec,
        messages: list[ParsedEmail],
        *,
        checkpoint_uid: int | None,
    ) -> PersistResult:
        self.persist_calls.append((account.account_id, list(messages), checkpoint_uid))
        return self.outcomes.get(account.account_id, PersistResult(inserted=len(messages)))


def _service(mailbox: FakeMailbox, store: FakeStore, **kwargs) -> IngestCoordinator:
    return IngestCoordinator(
        inbox=mailbox,
        store=store,
        policy=IngestPolicy(max_workers=2, timeout_seconds=1),
        **kwargs,
    )


async def test_incremental_sync_persists_and_advances_checkpoint() -> None:
    account = _account()
    mailbox = FakeMailbox({1: _read_result(1, 6, 7, 8)})
    store = FakeStore([account], outcomes={1: PersistResult(inserted=2, skipped=1)})

    report = await _service(mailbox, store).ingest(SyncRequest())

    assert mailbox.calls == [(1, 5, None)]
    assert [(uid.uid) for uid in store.persist_calls[0][1]] == [6, 7, 8]
    assert store.persist_calls[0][2] == 8
    assert report.total_inserted == 2
    assert report.total_skipped == 1
    assert report.total_failed == 0
    assert report.results[0].checkpoint_advanced is True


async def test_limit_persists_but_does_not_advance_checkpoint() -> None:
    account = _account()
    mailbox = FakeMailbox({1: _read_result(1, 6, 7)})
    store = FakeStore([account])

    report = await _service(mailbox, store).ingest(SyncRequest(limit=10))

    assert mailbox.calls == [(1, 5, 10)]
    assert store.persist_calls[0][2] is None
    assert report.results[0].checkpoint_advanced is False
    assert report.total_failed == 0


async def test_full_sync_starts_at_zero_and_never_moves_cursor_backwards() -> None:
    account = _account(last_sync_uid=5)
    mailbox = FakeMailbox({1: _read_result(1, 1, 2, 8)})
    store = FakeStore([account])

    report = await _service(mailbox, store).ingest(SyncRequest(full=True))

    assert mailbox.calls == [(1, 0, None)]
    assert store.persist_calls[0][2] == 8
    assert report.results[0].checkpoint_advanced is True


async def test_full_limit_mode_does_not_advance_checkpoint() -> None:
    account = _account()
    mailbox = FakeMailbox({1: _read_result(1, 1, 2)})
    store = FakeStore([account])

    await _service(mailbox, store).ingest(SyncRequest(full=True, limit=2))

    assert mailbox.calls == [(1, 0, 2)]
    assert store.persist_calls[0][2] is None


async def test_partial_fetch_persists_available_mail_but_holds_checkpoint() -> None:
    account = _account()
    mailbox = FakeMailbox({1: _read_result(1, 7, failed_uids=(6,))})
    store = FakeStore([account])

    report = await _service(mailbox, store).ingest(SyncRequest())

    assert [message.uid for message in store.persist_calls[0][1]] == [7]
    assert store.persist_calls[0][2] is None
    assert report.total_inserted == 1
    assert report.total_failed == 1
    assert report.results[0].failed_uids == (6,)
    assert "checkpoint was not advanced" in (report.results[0].error or "")


async def test_parse_failure_holds_checkpoint_but_keeps_other_messages() -> None:
    account = _account()
    mailbox = FakeMailbox({1: _read_result(1, 6, 7)})
    store = FakeStore([account])

    def parser(raw: RawEmail) -> ParsedEmail:
        if raw.uid == 6:
            raise ValueError("bad mail")
        return ParsedEmail(account_id=raw.account_id, uid=raw.uid, subject="ok")

    report = await _service(mailbox, store, parser=parser).ingest(SyncRequest())

    assert [message.uid for message in store.persist_calls[0][1]] == [7]
    assert store.persist_calls[0][2] is None
    assert report.results[0].failed_uids == (6,)


async def test_successful_empty_sync_refreshes_the_success_heartbeat() -> None:
    account = _account(last_sync_uid=5)
    mailbox = FakeMailbox({1: _read_result(1)})
    store = FakeStore([account])

    report = await _service(mailbox, store).ingest(SyncRequest())

    assert store.persist_calls == [(1, [], 5)]
    assert report.total_failed == 0


async def test_account_failure_is_isolated_from_other_accounts() -> None:
    accounts = [_account(1), _account(2)]
    mailbox = FakeMailbox({1: _read_result(1, 6), 2: RuntimeError("read boom")})
    store = FakeStore(accounts)

    report = await _service(mailbox, store).ingest(SyncRequest())

    assert report.total_inserted == 1
    assert report.total_failed == 1
    assert [result.account_id for result in report.results] == [1, 2]
    assert store.persist_calls[0][0] == 1


@pytest.mark.parametrize("limit", [0, -1])
async def test_ingest_rejects_non_positive_limit(limit: int) -> None:
    service = _service(FakeMailbox({}), FakeStore([]))

    with pytest.raises(ValueError, match="limit"):
        await service.ingest(SyncRequest(limit=limit))


async def test_invalid_mailbox_result_is_isolated_before_persisting() -> None:
    account = _account()
    duplicate_uid = RawEmail(account_id=account.account_id, uid=6, raw=b"one")
    mailbox = FakeMailbox(
        {
            account.account_id: MailboxReadResult(
                messages=(duplicate_uid, duplicate_uid),
            )
        }
    )
    store = FakeStore([account])

    report = await _service(mailbox, store).ingest(SyncRequest())

    assert store.persist_calls == []
    assert report.total_failed == 1
    assert report.results[0].error == "messages must not contain duplicate UIDs"


async def test_negative_persist_counts_are_isolated() -> None:
    account = _account()
    mailbox = FakeMailbox({account.account_id: _read_result(account.account_id, 6)})
    store = FakeStore(
        [account],
        outcomes={account.account_id: PersistResult(inserted=-1)},
    )

    report = await _service(mailbox, store).ingest(SyncRequest())

    assert report.total_inserted == 0
    assert report.total_failed == 1
    assert report.results[0].error == "inserted and skipped must be non-negative"
