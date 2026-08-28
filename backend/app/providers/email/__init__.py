"""Low-level mail protocol contracts.

Keep this package initializer light: importing a contract must not import the
IMAP SDK or select a production adapter. Concrete implementations and the
registry are imported explicitly by the composition root.
"""

from app.schemas.account import AccountConfig

from .base import MailClient, MailClientError, MailClientFactory, MailFetchResult

__all__ = [
    "AccountConfig",
    "MailClient",
    "MailClientError",
    "MailClientFactory",
    "MailFetchResult",
]
