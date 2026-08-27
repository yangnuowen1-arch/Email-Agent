"""Composition root for the process-level inbound-mail dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from app.core.settings import AppConfig
from app.db.email_sync_store import SqlAlchemyEmailSyncStore
from app.db.engine import Database, build_database
from app.observability import configure_logging
from app.ports import EmailSyncStore, InboundMailbox
from app.providers.email.imap_reader import ImapMailboxReader
from app.providers.email.registry import create_client
from app.services.ingest import IngestCoordinator, IngestPolicy


@dataclass(slots=True)
class Container:
    """Already-wired runtime dependencies for one CLI/API process.

    This type performs no business work. ``build_container`` is the one place
    that chooses production adapter implementations; tests can construct this
    dataclass with fakes instead.
    """

    config: AppConfig
    logger: Any
    database: Database
    inbox: InboundMailbox
    sync_store: EmailSyncStore
    mail_sync: IngestCoordinator

    async def close_all(self) -> None:
        """Release resources owned by this process-level composition root."""

        await self.database.dispose()


def build_container(config: AppConfig) -> Container:
    """Wire the concrete IMAP and SQLAlchemy adapters into application services."""

    configure_logging(config.log_level)
    logger = structlog.get_logger("email-agent")
    database = build_database(config)
    inbox = ImapMailboxReader(client_factory=create_client)
    sync_store = SqlAlchemyEmailSyncStore(database)
    mail_sync = IngestCoordinator(
        inbox=inbox,
        store=sync_store,
        policy=IngestPolicy(
            max_workers=config.sync_max_workers,
            timeout_seconds=config.sync_timeout_seconds,
        ),
    )
    return Container(
        config=config,
        logger=logger,
        database=database,
        inbox=inbox,
        sync_store=sync_store,
        mail_sync=mail_sync,
    )
