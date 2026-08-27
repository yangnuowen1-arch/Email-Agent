from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class RawEmail:
    """解析器的入参：单封原始 RFC822 字节及其同步上下文。"""

    account_id: int
    uid: int
    raw: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
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
