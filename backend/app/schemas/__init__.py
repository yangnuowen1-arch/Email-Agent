"""邮件相关跨层数据契约（schemas）。

所有跨层出入参（账号视图、原始邮件、解析结果、同步报告）集中在此包声明，
各层只 import 本包的契约类，避免 service 与 db 之间产生直接类型耦合。
"""

from app.schemas.email import (
    AccountSpec,
    AccountResult,
    BatchResult,
    EmailData,
    RawEmail,
)

__all__ = ["AccountSpec", "RawEmail", "EmailData", "AccountResult", "BatchResult"]
