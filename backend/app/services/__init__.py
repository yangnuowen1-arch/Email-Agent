"""确定性领域逻辑层的公开入口。"""

from app.services.ingest import IngestCoordinator, IngestPolicy
from app.services.mail_query import MailAccessDeniedError, MailQueryService
from app.services.parsing import parse_email

__all__ = [
    "IngestCoordinator",
    "IngestPolicy",
    "MailAccessDeniedError",
    "MailQueryService",
    "parse_email",
]
