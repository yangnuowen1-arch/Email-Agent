"""Low-level mail protocol contracts.

Keep this package initializer light: importing a contract must not import the
IMAP SDK or select a production adapter. Concrete implementations and the
registry are imported explicitly by the composition root.
"""

from .base import MailClient, MailClientError, MailClientFactory, MailFetchResult
from app.schemas.account import AccountConfig

__all__ = [
    "AccountConfig",
    "MailClient",
    "MailClientError",
    "MailClientFactory",
    "MailFetchResult",
]
