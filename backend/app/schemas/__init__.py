# 跨层数据契约：纯数据结构，不依赖 ORM、外部 SDK 或配置。
# 任何一层都可以 import 本包；本包不 import 任何 app 内部模块。
from app.schemas.account import AccountConfig
from app.schemas.email import (
    AccountSpec,
    EmailData,
    ParsedAttachment,
    ParsedEmail,
    RawEmail,
)

__all__ = [
    "AccountConfig",
    "ParsedEmail",
    "ParsedAttachment",
    "AccountSpec",
    "EmailData",
    "RawEmail",
]
