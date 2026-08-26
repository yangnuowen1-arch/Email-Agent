from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.models.base import Base

# 当前支持的邮件协议集合，新增协议时在此扩展，factory 会据此路由
_SUPPORTED_PROTOCOLS = {"imap"}


class Account(Base):
    """邮箱账号 ORM 模型，对应数据库 email_accounts 表的一行。

    既是领域契约（供 parser/client/service/cli 使用），也是持久化实体：
    通过 Session 查询即返回该对象，跨线程传值时因 expire_on_commit=False
    可直接读取已加载的标量字段（如 host/port/username 等）。
    """

    __tablename__ = "email_accounts"

    # 主键，自增 ID，外键关联 emails.account_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 账号别名，用于日志和汇总报告展示，便于运维识别
    name: Mapped[str] = mapped_column(String)
    # IMAP 服务器地址
    host: Mapped[str] = mapped_column(String)
    # 服务端口，SSL 场景默认 993
    port: Mapped[int] = mapped_column(Integer, default=993)
    # 协议标识，当前仅支持 imap，factory 据此创建具体 MailClient
    protocol: Mapped[str] = mapped_column(String, default="imap")
    # 登录用户名，通常为完整邮箱地址
    username: Mapped[str] = mapped_column(String)
    # 登录密码/授权码，禁止写入任何日志
    password: Mapped[str] = mapped_column(String)
    # 是否启用 SSL/TLS 连接
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    # 要拉取的邮箱文件夹，默认收件箱
    folder: Mapped[str] = mapped_column(String, default="INBOX")
    # 软开关：FALSE 的账号不会被调度
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 增量断点：已成功入库的最大 UID，0 表示从未同步（首跑全量）
    last_sync_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    # 最近一次成功同步时间，仅运维观测用
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __init__(
        self,
        *,
        id: int | None = None,
        name: str,
        host: str,
        username: str,
        password: str,
        port: int = 993,
        protocol: str = "imap",
        use_ssl: bool = True,
        folder: str = "INBOX",
        enabled: bool = True,
        last_sync_uid: int = 0,
        last_sync_at: datetime | None = None,
    ) -> None:
        """构造后校验：确保所有字段满足业务约束（与表默认值保持一致）。"""
        # id 必须为正整数，数据库自增主键不可能为 0 或负数
        if id is not None and (not isinstance(id, int) or id <= 0):
            msg = f"id must be positive int, got {id!r}"
            raise ValueError(msg)
        # 别名和服务器地址不能为空，否则无法建立连接和展示日志
        if not name:
            raise ValueError("name must be non-empty")
        if not host:
            raise ValueError("host must be non-empty")
        # 端口必须在 TCP 合法范围内 1~65535
        if not isinstance(port, int) or not 1 <= port <= 65535:
            msg = f"port must be int in 1..65535, got {port!r}"
            raise ValueError(msg)
        # 协议必须在支持列表中，否则 factory 无法创建客户端
        if protocol not in _SUPPORTED_PROTOCOLS:
            msg = f"protocol must be one of {sorted(_SUPPORTED_PROTOCOLS)}, got {protocol!r}"
            raise ValueError(msg)
        # 登录凭证不能为空
        if not username:
            raise ValueError("username must be non-empty")
        if not password:
            raise ValueError("password must be non-empty")
        # 断点 UID 必须为非负整数，0 代表从未同步
        if not isinstance(last_sync_uid, int) or last_sync_uid < 0:
            msg = f"last_sync_uid must be int >=0, got {last_sync_uid!r}"
            raise ValueError(msg)
        # 文件夹不能为空，IMAP 拉取时需要明确指定
        if not folder:
            raise ValueError("folder must be non-empty")

        self.id = id  # type: ignore[assignment]
        self.name = name
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.protocol = protocol
        self.use_ssl = use_ssl
        self.folder = folder
        self.enabled = enabled
        self.last_sync_uid = last_sync_uid
        self.last_sync_at = last_sync_at

    @validates("protocol")
    def _validate_protocol(self, key: str, value: str) -> str:
        # 与 __init__ 保持一致的协议白名单校验（ORM 加载/赋值时也会触发）
        if value not in _SUPPORTED_PROTOCOLS:
            msg = f"protocol must be one of {sorted(_SUPPORTED_PROTOCOLS)}, got {value!r}"
            raise ValueError(msg)
        return value

    @validates("last_sync_uid")
    def _validate_last_sync_uid(self, key: str, value: int) -> int:
        if not isinstance(value, int) or value < 0:
            msg = f"last_sync_uid must be int >=0, got {value!r}"
            raise ValueError(msg)
        return value
