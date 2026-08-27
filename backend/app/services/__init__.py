"""确定性领域逻辑层的公开入口。"""

from app.services.ingest import IngestCoordinator, IngestPolicy
from app.services.parsing import parse_email

__all__ = ["IngestCoordinator", "IngestPolicy", "parse_email"]
