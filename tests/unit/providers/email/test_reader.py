"""Tests for the IMAP adapter that implements the inbound mailbox port."""

from __future__ import annotations

import pytest

from app.providers.email.base import MailClientError, MailFetchResult
from app.providers.email.imap_reader import ImapMailboxReader
from app.schemas import AccountSpec


class FakeClient:
    def __init__(self, result: MailFetchResult | Exception) -> None:
        self.result = result
        self.connect_calls = 0
        self.close_calls = 0
        self.fetch_calls: list[tuple[str, int, int | None]] = []

    def connect(self) -> None:
        self.connect_calls += 1

    def fetch_emails(
        self, folder: str, since_uid: int, limit: int | None = None
    ) -> MailFetchResult:
        self.fetch_calls.append((folder, since_uid, limit))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        self.close_calls += 1


def _account() -> AccountSpec:
    return AccountSpec(
        account_id=1,
        name="acc",
        last_sync_uid=5,
        host="imap.example.com",
        username="u",
        password="p",
    )


async def test_reader_maps_protocol_result_to_raw_mail_contract() -> None:
    client = FakeClient(MailFetchResult(messages=((6, b"one"), (7, b"two"))))
    reader = ImapMailboxReader(client_factory=lambda _account: client)

    result = await reader.read(_account(), after_uid=5, limit=2)

    assert [(message.account_id, message.uid, message.raw) for message in result.messages] == [
        (1, 6, b"one"),
        (1, 7, b"two"),
    ]
    assert result.failed_uids == ()
    assert client.fetch_calls == [("INBOX", 5, 2)]
    assert client.connect_calls == 1
    assert client.close_calls == 1


async def test_reader_preserves_failed_uids_for_checkpoint_safety() -> None:
    client = FakeClient(MailFetchResult(messages=((7, b"two"),), failed_uids=(6,)))
    reader = ImapMailboxReader(client_factory=lambda _account: client)

    result = await reader.read(_account(), after_uid=5, limit=None)

    assert [message.uid for message in result.messages] == [7]
    assert result.failed_uids == (6,)


async def test_reader_propagates_sanitized_client_error_and_still_closes() -> None:
    client = FakeClient(MailClientError("[acc] connect failed"))
    reader = ImapMailboxReader(client_factory=lambda _account: client)

    with pytest.raises(MailClientError, match="connect failed"):
        await reader.read(_account(), after_uid=5, limit=None)

    assert client.close_calls == 1


@pytest.mark.parametrize("after_uid, limit", [(-1, None), (0, 0)])
async def test_reader_rejects_invalid_boundary_or_limit(after_uid: int, limit: int | None) -> None:
    client = FakeClient(MailFetchResult())
    reader = ImapMailboxReader(client_factory=lambda _account: client)

    with pytest.raises(ValueError):
        await reader.read(_account(), after_uid=after_uid, limit=limit)
