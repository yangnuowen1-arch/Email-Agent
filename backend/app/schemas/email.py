from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TypeAlias


@dataclass(frozen=True, kw_only=True)
class RawEmail:
    """解析器的入参：单封原始 RFC822 字节及其同步上下文。"""

    account_id: int
    uid: int
    raw: bytes


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


# ``EmailData`` 是合并前已公开使用的名称。保留它作为同一 DTO 的
# 兼容别名，避免服务层、测试和下游调用方同时出现两种不同的邮件类型。
EmailData: TypeAlias = ParsedEmail


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
