"""Mail client registration and construction."""

from __future__ import annotations

from .base import AccountConfig, MailClient
from .imap_client import ImapMailClient

__all__ = ["create_client", "register_client"]


# 协议注册表：协议名（小写）→ 对应的 MailClient 子类。
# 新增协议只需在此注册，调度层零改动。
_REGISTRY: dict[str, type[MailClient]] = {"imap": ImapMailClient}


def register_client(protocol: str, cls: type[MailClient]) -> None:
    """注册新的协议实现，供扩展协议时调用。

    Args:
        protocol: 协议名，大小写不敏感，如 ``"imap"``。
        cls: :class:`MailClient` 的子类。

    Raises:
        TypeError: 若 ``cls`` 不是 ``MailClient`` 子类。
        ValueError: 若协议名为空。
    """
    if not protocol or not protocol.strip():
        raise ValueError("protocol must be non-empty")

    if not isinstance(cls, type) or not issubclass(cls, MailClient):
        msg = f"cls must be a MailClient subclass, got {cls!r}"
        raise TypeError(msg)

    # 统一转为小写存储，实现大小写不敏感的查找；
    # 允许覆盖内置注册以支持定制。
    _REGISTRY[protocol.strip().lower()] = cls


def create_client(account: AccountConfig) -> MailClient:
    """根据账号的 protocol 字段创建对应的 MailClient 实例。

    Args:
        account: 含 ``protocol`` 字段的账号对象，大小写不敏感。

    Returns:
        绑定到 ``account`` 的具体 ``MailClient`` 实例。

    Raises:
        ValueError: 若协议不受支持。
    """
    proto = str(account.protocol).strip().lower()
    cls = _REGISTRY.get(proto)

    # 错误信息保留原始大小写，便于定位配置错误
    if cls is None:
        raise ValueError(f"unsupported protocol {account.protocol!r}")

    return cls(account)
