# 邮件协议抽象层对外导出，service 层仅依赖此抽象
from email_agent.email_clients.base import AccountConfig, MailClient, MailClientError
from email_agent.email_clients.factory import create_client, register_client
from email_agent.email_clients.imap.client import ImapMailClient

__all__ = [
    "AccountConfig",
    "ImapMailClient",
    "MailClient",
    "MailClientError",
    "create_client",
    "register_client",
]
