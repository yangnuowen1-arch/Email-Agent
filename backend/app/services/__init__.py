"""确定性领域逻辑层的公开入口。"""

from app.services.ingest import IngestCoordinator, IngestPolicy
from app.services.mail_query import MailAccessDeniedError, MailQueryService
from app.services.mail_workflow import (
    AnalysisEmailMismatchError,
    AnalysisNotFoundError,
    ArchivedEmailNotFoundError,
    DraftRevisionNotAllowedError,
    DraftStateTransitionError,
    DraftStoreContractError,
    DraftVersionConflictError,
    MailAnalysisService,
    MailWorkflowError,
    ReplyDraftNotFoundError,
    ReplyDraftService,
)
from app.services.parsing import parse_email

__all__ = [
    "IngestCoordinator",
    "IngestPolicy",
    "MailAccessDeniedError",
    "MailQueryService",
    "MailWorkflowError",
    "ArchivedEmailNotFoundError",
    "AnalysisNotFoundError",
    "AnalysisEmailMismatchError",
    "ReplyDraftNotFoundError",
    "DraftStateTransitionError",
    "DraftRevisionNotAllowedError",
    "DraftVersionConflictError",
    "DraftStoreContractError",
    "MailAnalysisService",
    "ReplyDraftService",
    "parse_email",
]
