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
    id: int | None = None
    message_id: str | None = None
    subject: str = ""
    sender: str | None = None
    recipients: list[str] = field(default_factory=list)
    sent_at: datetime | None = None
    text_body: str | None = None
    html_body: str | None = None
    is_read: bool = False
    fetched_at: datetime | None = None
    analysis: dict | None = None


@dataclass(frozen=True, kw_only=True)
class AccountSpec:
    """供 services / providers 使用的账号只读视图（解耦 services 与 db）。

    由 ``core.listener._to_account_spec`` 从 ORM ``Account`` 投影而来；字段与
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
class ParsedAttachment:
    """一个已解析的邮件附件，parser 的产出。

    ``kind`` 取值：image（图片，含内嵌 cid 图）/ email（.eml / message/rfc822）/
    document（其他）。``content`` 为附件原始字节，仅在内存中传递、不落库；
    超过大小上限或解析失败时为 None（只保留元数据）。
    """

    filename: str = ""
    content_type: str = ""
    disposition: str | None = None
    content_id: str | None = None
    size: int = 0
    content: bytes | None = None
    kind: str = "document"


@dataclass(frozen=True, kw_only=True)
class EmailData:
    """一封已解析的领域邮件，parser 的产出、repository 的入参。

    字段与 ``emails`` 表当前列一一对应；``fetched_at`` 缺省为 None，
    由落库层（ORM ``EmailMessage``）在写入时按当前 UTC 时间填充。
    附件字节不落库，由落库层上传对象存储后仅保留引用（见 EmailAttachment）。
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
    is_read: bool = False
    fetched_at: datetime | None = None
    attachments: list[ParsedAttachment] = field(default_factory=list)
