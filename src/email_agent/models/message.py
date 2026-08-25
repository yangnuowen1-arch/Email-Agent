from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import ARRAY, BigInteger, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, validates

from email_agent.models.base import Base


def _now_utc() -> datetime:
    """返回当前 UTC 时间，作为 fetched_at 的默认工厂。"""
    # 使用 UTC 时区，避免服务器本地时区差异导致的时间混乱
    return datetime.now(UTC)


class EmailMessage(Base):
    """已解析邮件 ORM 模型，对应数据库 emails 表的一行。

    parser 解析出的对象即该模型的瞬时实例（未绑定 Session），
    由 repository 通过幂等批量插入写入数据库。
    """

    __tablename__ = "emails"
    # (account_id, uid) 唯一约束保证同一邮箱内邮件不重复入库
    __table_args__ = (
        UniqueConstraint("account_id", "uid", name="emails_account_id_uid_key"),
    )

    # 数据库自增主键，插入前为 None
    id: Mapped[int | None] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 所属账号 ID，外键关联 email_accounts.id
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 邮箱服务器内该文件夹下的 UID，与 account_id 组成幂等键
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # RFC822 Message-ID 头，便于跨系统追溯去重，可能缺失
    message_id: Mapped[str | None] = mapped_column(String)
    # 已解码的主题，解析失败时存空串而非抛异常
    subject: Mapped[str] = mapped_column(String, default="")
    # 发件人地址
    sender: Mapped[str | None] = mapped_column(String)
    # 收件人列表（To + Cc 合并存储）
    recipients: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    # 邮件 Date 头解析结果，可能缺失或格式非法
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 纯文本正文
    text_body: Mapped[str | None] = mapped_column(Text)
    # HTML 正文
    html_body: Mapped[str | None] = mapped_column(Text)
    # 本地拉取时间，默认为当前 UTC 时间
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_now_utc)

    def __init__(
        self,
        *,
        id: int | None = None,
        account_id: int,
        uid: int,
        message_id: str | None = None,
        subject: str | None = "",
        sender: str | None = None,
        recipients: list[str] | None = None,
        sent_at: datetime | None = None,
        text_body: str | None = None,
        html_body: str | None = None,
        fetched_at: datetime | None = None,
    ) -> None:
        """构造后校验与容错归一：确保核心字段类型正确，缺失字段给予默认值。"""
        # account_id 和 uid 是幂等键的核心，必须为正整数/非负整数
        if not isinstance(account_id, int) or account_id <= 0:
            msg = f"account_id must be positive int, got {account_id!r}"
            raise ValueError(msg)
        if not isinstance(uid, int) or uid < 0:
            msg = f"uid must be int >=0, got {uid!r}"
            raise ValueError(msg)
        # subject 可能被外部传入 None，做容错归一为空串，避免后续拼接/展示报错
        if subject is None:
            subject = ""
        if not isinstance(subject, str):
            msg = f"subject must be str, got {type(subject).__name__}"
            raise TypeError(msg)
        # recipients 同理，None 归一为空列表
        if recipients is None:
            recipients = []
        if not isinstance(recipients, list):
            msg = "recipients must be list[str]"
            raise TypeError(msg)

        self.id = id
        self.account_id = account_id
        self.uid = uid
        self.message_id = message_id
        self.subject = subject
        self.sender = sender
        self.recipients = recipients
        self.sent_at = sent_at
        self.text_body = text_body
        self.html_body = html_body
        # fetched_at 缺省时按构造时间填充（与入库时间一致）
        self.fetched_at = fetched_at if fetched_at is not None else _now_utc()

    @validates("account_id")
    def _validate_account_id(self, key: str, value: int) -> int:
        if not isinstance(value, int) or value <= 0:
            msg = f"account_id must be positive int, got {value!r}"
            raise ValueError(msg)
        return value

    @validates("uid")
    def _validate_uid(self, key: str, value: int) -> int:
        if not isinstance(value, int) or value < 0:
            msg = f"uid must be int >=0, got {value!r}"
            raise ValueError(msg)
        return value

    @validates("subject")
    def _validate_subject(self, key: str, value: str | None) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            msg = f"subject must be str, got {type(value).__name__}"
            raise TypeError(msg)
        return value

    @validates("recipients")
    def _validate_recipients(self, key: str, value: list[str] | None) -> list[str] | None:
        if value is None:
            return []
        if not isinstance(value, list):
            msg = "recipients must be list[str]"
            raise TypeError(msg)
        return value
