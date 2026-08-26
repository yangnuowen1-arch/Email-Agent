from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, kw_only=True)
class AccountSpec:
    """服务层读取邮件所需的账号视图，与 ORM Account 解耦。"""

    account_id: int
    name: str
    last_sync_uid: int
    host: str
    username: str
    password: str
    port: int = 993
    protocol: str = "imap"
    use_ssl: bool = True
    folder: str = "INBOX"


@dataclass(frozen=True, kw_only=True)
class RawEmail:
    """解析器的入参：单封原始 RFC822 字节及其上下文。"""

    account_id: int
    uid: int
    raw: bytes


@dataclass(frozen=True, kw_only=True)
class EmailData:
    """解析后的邮件数据，services 层对外的统一返回对象。"""

    account_id: int
    uid: int
    message_id: str | None = None
    subject: str = ""
    sender: str | None = None
    recipients: list[str] = field(default_factory=list)
    sent_at: datetime | None = None
    text_body: str | None = None
    html_body: str | None = None
    fetched_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class AccountResult:
    """单账号同步结果，用于上层汇总与失败定位。"""

    account_id: int
    name: str
    inserted: int = 0
    skipped: int = 0
    error: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True, kw_only=True)
class BatchResult:
    """批量同步的汇总报告。"""

    results: list[AccountResult] = field(default_factory=list)
    total_inserted: int = 0
    total_skipped: int = 0
    total_failed: int = 0
    duration_ms: int = 0
