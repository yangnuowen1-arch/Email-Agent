# 邮件协议抽象层对外导出，service 层仅依赖此抽象
from email_agent.email_clients.base import MailClient, MailClientError  # 抽象基类与统一异常
from email_agent.email_clients.factory import create_client, register_client  # 工厂与注册表
from email_agent.email_clients.imap.client import ImapMailClient  # IMAP 首个具体实现

__all__ = ["ImapMailClient", "MailClient", "MailClientError", "create_client", "register_client"]
