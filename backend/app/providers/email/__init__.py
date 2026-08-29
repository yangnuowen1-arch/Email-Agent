# 邮件协议抽象层对外导出，service 层仅依赖此抽象
from app.providers.email.base import (  # noqa: F401  RawBatch/BatchCallback 供调用方做类型标注
    BatchCallback,
    MailClient,
    MailClientAuthError,
    MailClientError,
    RawBatch,
)
from app.providers.email.factory import create_client, register_client
from app.providers.email.imap.client import ImapMailClient
from app.schemas.account import AccountConfig

__all__ = [
    "AccountConfig",
    "BatchCallback",
    "ImapMailClient",
    "MailClient",
    "MailClientAuthError",
    "MailClientError",
    "RawBatch",
    "create_client",
    "register_client",
]
