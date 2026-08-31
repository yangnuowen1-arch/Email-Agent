"""Composition root for the process-level inbound-mail dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from app.core.settings import AppConfig
from app.db.email_query_store import SqlAlchemyMailQueryStore
from app.db.email_sync_store import SqlAlchemyEmailSyncStore
from app.db.engine import Database, build_database
from app.db.mail_workflow_store import SqlAlchemyMailWorkflowStore
from app.llm import GatewayMailAnalyzer, GatewayReplyDraftGenerator, LLMGateway
from app.observability import configure_logging
from app.ports import (
    EmailSyncStore,
    InboundMailbox,
    MailAnalysisStore,
    MailQueryStore,
    ReplyDraftStore,
)
from app.providers.email.imap_reader import ImapMailboxReader
from app.providers.email.registry import create_client
from app.services.ingest import IngestCoordinator, IngestPolicy
from app.services.mail_query import MailQueryService
from app.services.mail_workflow import MailAnalysisService, ReplyDraftService


@dataclass(frozen=True, slots=True)
class MailWorkflowServices:
    """Request-scoped use cases backed by an explicitly injected LLM gateway.

    The process container deliberately does not select a concrete model
    provider.  An authenticated API or worker supplies its approved gateway,
    while these services retain the container's database-backed scope and
    approval boundaries.
    """

    analysis: MailAnalysisService
    drafts: ReplyDraftService


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
    mail_query_store: MailQueryStore
    mail_query: MailQueryService
    mail_analysis_store: MailAnalysisStore
    reply_draft_store: ReplyDraftStore

    def build_mail_workflow(self, gateway: LLMGateway) -> MailWorkflowServices:
        """Create analysis/draft use cases without granting SMTP capability.

        The default adapters ask the injected gateway only for typed proposals.
        Account scope is still supplied per service call, and the resulting
        draft can only enter ``approved`` through ``ReplyDraftService.decide``.
        """

        analysis = MailAnalysisService(
            self.mail_query,
            GatewayMailAnalyzer(gateway),
            self.mail_analysis_store,
        )
        drafts = ReplyDraftService(
            self.mail_query,
            self.mail_analysis_store,
            GatewayReplyDraftGenerator(gateway),
            self.reply_draft_store,
        )
        return MailWorkflowServices(analysis=analysis, drafts=drafts)

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
    mail_query_store = SqlAlchemyMailQueryStore(database)
    mail_workflow_store = SqlAlchemyMailWorkflowStore(database)
    mail_sync = IngestCoordinator(
        inbox=inbox,
        store=sync_store,
        policy=IngestPolicy(
            max_workers=config.sync_max_workers,
            timeout_seconds=config.sync_timeout_seconds,
        ),
    )
    mail_query = MailQueryService(mail_query_store)
    return Container(
        config=config,
        logger=logger,
        database=database,
        inbox=inbox,
        sync_store=sync_store,
        mail_sync=mail_sync,
        mail_query_store=mail_query_store,
        mail_query=mail_query,
        mail_analysis_store=mail_workflow_store,
        reply_draft_store=mail_workflow_store,
    )
