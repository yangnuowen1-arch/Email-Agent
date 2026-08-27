"""SQLAlchemy adapter for the inbound mail-sync storage port."""

from __future__ import annotations

from collections.abc import Sequence

from app.db.db import Account, EmailMessage
from app.db.engine import Database
from app.db.repositories import EmailAccountRepository, EmailRepository
from app.schemas import AccountSpec, ParsedEmail, PersistResult


def _to_account_spec(account: Account) -> AccountSpec:
    """Project a persistence model into the contract needed by mail adapters."""

    return AccountSpec(
        account_id=account.id,
        name=account.name,
        last_sync_uid=account.last_sync_uid,
        host=account.host,
        username=account.username,
        password=account.password,
        port=account.port,
        protocol=account.protocol,
        use_ssl=account.use_ssl,
        folder=account.folder,
    )


def _to_orm(message: ParsedEmail) -> EmailMessage:
    """Keep ORM mapping at the SQLAlchemy boundary, not in the service."""

    return EmailMessage(
        account_id=message.account_id,
        uid=message.uid,
        message_id=message.message_id,
        subject=message.subject,
        sender=message.sender,
        recipients=message.recipients,
        sent_at=message.sent_at,
        text_body=message.text_body,
        html_body=message.html_body,
        fetched_at=message.fetched_at,
    )


class SqlAlchemyEmailSyncStore:
    """Persist mail and checkpoint updates in one Database.session transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_enabled_accounts(self) -> list[AccountSpec]:
        async with self._database.session() as session:
            accounts = await EmailAccountRepository(session).list_account(enabled_only=True)
        return [_to_account_spec(account) for account in accounts]

    async def persist(
        self,
        account: AccountSpec,
        messages: Sequence[ParsedEmail],
        *,
        checkpoint_uid: int | None,
    ) -> PersistResult:
        """Idempotently store mail and optionally mark a successful sync.

        ``checkpoint_uid`` may equal the existing cursor.  In that case the
        account's ``last_sync_at`` is still refreshed to record a successful
        no-new-mail sync.  A ``None`` checkpoint deliberately performs no
        account update, which is required for limit and partial-failure modes.
        """

        if checkpoint_uid is not None and checkpoint_uid < account.last_sync_uid:
            msg = "checkpoint_uid must not move an account cursor backwards"
            raise ValueError(msg)

        async with self._database.session() as session:
            inserted = await EmailRepository(session).bulk_create_email(
                [_to_orm(message) for message in messages]
            )
            if checkpoint_uid is not None:
                await EmailAccountRepository(session).update_account_checkpoint(
                    account.account_id,
                    checkpoint_uid,
                )

        return PersistResult(inserted=inserted, skipped=len(messages) - inserted)
