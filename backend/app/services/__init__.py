"""确定性领域逻辑层的公开入口。"""

from app.services.email import EmailService
from app.services.parsing import parse_email

__all__ = ["EmailService", "parse_email"]
