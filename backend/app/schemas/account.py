from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class AccountConfig:
    """建立邮件连接所需的账号信息，不可变。

    由调用方从 ORM ``Account`` 投影而来，供 providers 层使用；
    providers 因此不需要认识任何持久化模型。
    """

    name: str
    host: str
    username: str
    password: str

    port: int = 993
    protocol: str = "imap"
    use_ssl: bool = True
    folder: str = "INBOX"
