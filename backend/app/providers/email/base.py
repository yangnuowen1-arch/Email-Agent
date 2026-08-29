from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from types import TracebackType

from app.schemas.account import AccountConfig

__all__ = [
    "AccountConfig",
    "MailClient",
    "MailClientAuthError",
    "MailClientError",
]

#: 一批待回调的原始邮件： ``(uid, raw RFC822 字节)`` 列表，按 uid 升序
RawBatch = list[tuple[int, bytes]]

#: 新邮件批次回调：实现方保证仅在批次可被安全重试时抛异常
BatchCallback = Callable[[RawBatch], None]


class MailClientError(Exception):
    """邮件客户端操作失败时抛出的统一异常。

    异常信息必须包含 ``account.name`` 以便定位是哪个账号失败，
    且绝不能包含 ``account.password``，避免敏感信息泄露到日志中。
    """


class MailClientAuthError(MailClientError):
    """认证/登录失败等不可恢复错误。

    自动重连对此类错误无意义，实现方应直接向上抛出终止接收，
    由调用方决定是否修正配置后重启监听。
    """


class MailClient(ABC):
    """邮件客户端抽象基类，定义所有协议必须实现的接口。

    子类需实现 ``connect`` / ``receive_emails`` / ``close``，
    分别对应 IMAP 等具体协议的连接、阻塞式接收、关闭逻辑。

    基类已提供 ``__enter__`` / ``__exit__``，调用方可直接使用
    ``with create_client(account) as client:`` 的上下文管理写法。
    """

    def __init__(self, account: AccountConfig) -> None:
        # 原样保存传入对象：保持与调用方同一实例，便于断言与字段复用
        self.account: AccountConfig = account

    @abstractmethod
    def connect(self) -> None:
        """建立连接并完成认证。"""

    @abstractmethod
    def receive_emails(
        self,
        folder: str,
        on_batch: BatchCallback,
        stop_event: threading.Event | None = None,
    ) -> None:
        """阻塞式接收新邮件，新邮件经 ``on_batch`` 回调推送。

        约定：

        - 启动时以文件夹当前状态为基线，只推送监听开始之后新到的邮件
        - 新邮件按 uid 升序分批回调 ``(uid, raw_bytes)``；回调成功返回后批次
          才算确认，回调抛异常则该批不确认，之后自动重推（调用方落库需幂等）
        - 连接级故障由实现方内部自动重连（指数退避），重连后从已确认的最大
          uid 继续，不丢不重
        - 登录失败等不可恢复错误抛 :class:`MailClientAuthError`
        - ``stop_event`` 置位后尽快返回

        Args:
            folder: 邮箱文件夹，如 ``INBOX``。
            on_batch: 批次回调，在实现方的接收线程内同步调用。
            stop_event: 置位后要求尽快停止接收并返回。
        """

    @abstractmethod
    def close(self) -> None:
        """关闭连接，需保证幂等（多次调用不报错）。"""

    def __enter__(self) -> MailClient:
        # 进入上下文时自动连接，简化调用方的样板代码
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        try:
            self.close()

        except Exception:
            # close 失败不能掩盖上下文内的原始异常；
            # 但正常退出路径上必须抛出，让调用方感知资源释放失败
            if exc_type is None:
                raise

        # 返回 False 表示不吞掉原始异常，交由外层处理
        return False
