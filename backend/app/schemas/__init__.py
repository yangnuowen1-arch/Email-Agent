"""跨层数据契约：纯数据结构，不依赖 ORM、外部 SDK 或配置。"""

from app.schemas.account import AccountConfig, AccountSpec
from app.schemas.email import ParsedEmail, RawEmail
from app.schemas.mail_query import MailContext, MailSearchCriteria, MailSearchItem
from app.schemas.sync import (
    AccountSyncResult,
    MailboxReadResult,
    PersistResult,
    SyncReport,
    SyncRequest,
)

__all__ = [
    "AccountConfig",
    "AccountSpec",
    "RawEmail",
    "ParsedEmail",
    "MailContext",
    "MailSearchCriteria",
    "MailSearchItem",
    "AccountSyncResult",
    "MailboxReadResult",
    "PersistResult",
    "SyncReport",
    "SyncRequest",
]
