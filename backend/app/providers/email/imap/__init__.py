# IMAP 子包对外导出
from app.providers.email.imap.client import (  # IMAP 客户端、默认连接超时与 IDLE 默认 ping 周期
    DEFAULT_IDLE_PING_INTERVAL,
    DEFAULT_TIMEOUT,
    ImapMailClient,
)

__all__ = ["DEFAULT_IDLE_PING_INTERVAL", "DEFAULT_TIMEOUT", "ImapMailClient"]
