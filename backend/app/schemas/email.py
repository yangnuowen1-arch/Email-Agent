from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, kw_only=True)
class ParsedEmail:
    """一封已解析的邮件，parser 的产出、repository 的入参。

    字段与 ``emails`` 表当前列一一对应，但本类型不感知数据库：
    ORM 映射只发生在 repository 层。
    """

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


@dataclass(frozen=True, kw_only=True)
class AccountSpec:
    """供 services / providers 使用的账号只读视图（解耦 services 与 db）。

    由 ``core.ingest._to_spec`` 从 ORM ``Account`` 投影而来；字段与
    ``schemas.account.AccountConfig`` 高度重合，使 provider 客户端无需认识持久化模型。
    """

    account_id: int
    name: str
    last_sync_uid: int = 0
    host: str
    username: str
    password: str
    port: int = 993
    protocol: str = "imap"
    use_ssl: bool = True
    folder: str = "INBOX"


@dataclass(frozen=True, kw_only=True)
class RawEmail:
    """一封待解析的原始邮件，parser 的入参。

    ``raw`` 为完整的 RFC822 字节，与解析结果解耦。
    """

    account_id: int
    uid: int
    raw: bytes


@dataclass(frozen=True, kw_only=True)
class EmailData:
    """一封已解析的领域邮件，parser 的产出、repository 的入参。

    字段与 ``emails`` 表当前列一一对应；``fetched_at`` 缺省为 None，
    由落库层（ORM ``EmailMessage``）在写入时按当前 UTC 时间填充。
    """

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
