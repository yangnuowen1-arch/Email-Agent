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


@dataclass(frozen=True, kw_only=True)
class AccountSpec(AccountConfig):
    """一次同步所需的账号视图。

    在连接配置之外，服务层还需要账号标识和增量断点；继承
    :class:`AccountConfig` 可让 provider 继续只依赖连接配置契约。
    """

    account_id: int
    last_sync_uid: int
