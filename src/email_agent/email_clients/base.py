from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import TracebackType


@dataclass(frozen=True)
class AccountConfig:
    """邮件客户端所需的账号连接信息，本包自包含的数据契约。

    字段与 ORM 的 ``Account`` 同名同义：真实 ORM 实例鸭子类型兼容，
    可直接传入，无需在本包内反向依赖 models 层。
    """

    name: str
    host: str
    username: str
    password: str

    port: int = 993
    protocol: str = "imap"
    use_ssl: bool = True
    folder: str = "INBOX"


class MailClientError(Exception):
    """邮件客户端操作失败时抛出的统一异常。

    异常信息必须包含 ``account.name`` 以便定位是哪个账号失败，
    且绝不能包含 ``account.password``，避免敏感信息泄露到日志中。
    """


class MailClient(ABC):
    """邮件客户端抽象基类，定义所有协议必须实现的接口。

    子类需实现 ``connect`` / ``fetch_emails`` / ``close``，
    分别对应 IMAP 等具体协议的连接、拉取、关闭逻辑。

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
    def fetch_emails(
        self,
        folder: str,
        since_uid: int,
        limit: int | None = None,
    ) -> list[tuple[int, bytes]]:
        """拉取指定文件夹中 UID 大于 since_uid 的原始邮件。

        Args:
            folder: 邮箱文件夹，如 ``INBOX``。
            since_uid: 上次同步的最大 UID；``0`` 表示全量拉取。
            limit: 可选的返回数量上限，按 UID 升序截断，用于调试/首跑限量。

        Returns:
            ``(uid, raw_bytes)`` 列表，按 uid 升序排列，raw_bytes 为完整 RFC822 字节。
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
