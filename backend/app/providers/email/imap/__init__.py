# IMAP 子包对外导出
from app.providers.email.imap.client import (  # IMAP 客户端与默认超时
    DEFAULT_TIMEOUT,
    ImapMailClient,
)

__all__ = ["DEFAULT_TIMEOUT", "ImapMailClient"]
