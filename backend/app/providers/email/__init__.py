# 邮件协议抽象层对外导出，service 层仅依赖此抽象
from app.providers.email.base import AccountConfig, MailClient, MailClientError
from app.providers.email.factory import create_client, register_client
from app.providers.email.imap.client import ImapMailClient

__all__ = [
    "AccountConfig",
    "ImapMailClient",
    "MailClient",
    "MailClientError",
    "create_client",
    "register_client",
]
