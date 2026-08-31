"""Application-owned ports for replaceable external capabilities."""

from app.ports.inbound_mail import InboundMailbox
from app.ports.mail_query import MailQueryStore
from app.ports.mail_workflow import (
    ArchivedMailContextReader,
    DraftVersionConflictError,
    MailAnalysisStore,
    MailAnalyzer,
    MailWorkflowError,
    ReplyDraftGenerator,
    ReplyDraftStore,
)
from app.ports.sync_store import EmailSyncStore

__all__ = [
    "ArchivedMailContextReader",
    "DraftVersionConflictError",
    "EmailSyncStore",
    "InboundMailbox",
    "MailAnalysisStore",
    "MailAnalyzer",
    "MailQueryStore",
    "MailWorkflowError",
    "ReplyDraftGenerator",
    "ReplyDraftStore",
]
