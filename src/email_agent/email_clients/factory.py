from __future__ import annotations

from typing import TYPE_CHECKING

from email_agent.email_clients.base import MailClient

if TYPE_CHECKING:
    from email_agent.models.account import Account

# 协议注册表：协议名（小写）→ 对应的 MailClient 子类
# 通过注册表实现协议适配器模式，新增协议只需注册，无需改动调度层
_REGISTRY: dict[str, type[MailClient]] = {}


def _lazy_register_defaults() -> None:
    """首次使用时懒加载注册内置协议，避免循环导入。"""
    # 已注册则直接返回，避免重复导入和覆盖用户自定义注册
    if _REGISTRY:
        return
    # 延迟导入 ImapMailClient，打破 factory 与 imap 具体实现之间的循环依赖
    from email_agent.email_clients.imap.client import ImapMailClient

    _REGISTRY["imap"] = ImapMailClient


def register_client(protocol: str, cls: type[MailClient]) -> None:
    """注册新的协议实现，供扩展协议时调用。

    Args:
        protocol: 协议名，大小写不敏感，如 ``"imap"``。
        cls: :class:`MailClient` 的子类。

    Raises:
        TypeError: 若 ``cls`` 不是 ``MailClient`` 子类。
        ValueError: 若协议名为空。
    """
    # 校验协议名非空
    if not protocol or not protocol.strip():
        raise ValueError("protocol must be non-empty")
    # 校验类继承关系，确保注册的是合法的邮件客户端
    if not isinstance(cls, type) or not issubclass(cls, MailClient):
        msg = f"cls must be a MailClient subclass, got {cls!r}"
        raise TypeError(msg)
    # 确保内置协议已注册，避免用户注册后覆盖时丢失默认项
    _lazy_register_defaults()
    # 统一转为小写存储，实现大小写不敏感的查找
    key = protocol.strip().lower()
    _REGISTRY[key] = cls


def create_client(account: Account) -> MailClient:
    """根据账号的 protocol 字段创建对应的 MailClient 实例。

    Args:
        account: 包含 ``protocol`` 字段的账号对象，大小写不敏感。

    Returns:
        绑定到 ``account`` 的具体 ``MailClient`` 实例。

    Raises:
        ValueError: 若协议不受支持。
    """
    # 确保内置协议已注册
    _lazy_register_defaults()
    # 统一转为小写进行查找，兼容 "IMAP" / "Imap" 等写法
    proto = str(getattr(account, "protocol", "")).strip().lower()
    cls = _REGISTRY.get(proto)
    if cls is None:
        # 保留原始大小写用于错误提示，便于用户定位配置错误
        raw = getattr(account, "protocol", proto)
        raise ValueError(f"unsupported protocol {raw!r}")
    # 实例化具体客户端，传入账号配置
    return cls(account)


# 模块导入时立即注册默认协议，保证正常使用时无需手动调用 _lazy_register_defaults
_lazy_register_defaults()
