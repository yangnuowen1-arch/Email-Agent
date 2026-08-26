from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.message import EmailMessage

if TYPE_CHECKING:
    pass


class EmailStore:
    """emails 表的访问入口：绑定到一个 Session。

    批量写入使用 PostgreSQL 的 ``INSERT ... ON CONFLICT DO NOTHING``
    实现幂等，重复执行不会产生重复数据。
    """

    def __init__(self, session: Session) -> None:
        # 绑定调用方提供的 Session（与 AccountStore 共享，保证事务原子性）
        self._s = session

    def bulk_insert(self, messages: list[EmailMessage]) -> int:
        """批量插入邮件，利用幂等键去重，返回实际插入的行数。"""
        # 空列表直接返回，避免执行无意义的 SQL
        if not messages:
            return 0

        # 组装批量插入的值列表，每行对应一封邮件的字段
        values: list[dict] = []
        for m in messages:
            # 类型检查，确保调用方传入的是领域模型，而非原始字典
            if not isinstance(m, EmailMessage):
                msg = f"expected EmailMessage, got {type(m).__name__}"
                raise TypeError(msg)
            # fetched_at 若为空则補为当前时间，保证入库时间可追溯
            fetched_at = m.fetched_at or datetime.now(UTC)
            values.append(
                {
                    "account_id": m.account_id,
                    "uid": m.uid,
                    "message_id": m.message_id,
                    "subject": m.subject,
                    "sender": m.sender,
                    "recipients": m.recipients,
                    "sent_at": m.sent_at,
                    "text_body": m.text_body,
                    "html_body": m.html_body,
                    "fetched_at": fetched_at,
                }
            )

        # 使用 ON CONFLICT DO NOTHING 实现幂等写入
        # (account_id, uid) 是唯一约束，重复执行不会产生重复数据
        # 通过 RETURNING 统计实际插入行数：被冲突跳过的行不会出现在返回结果中
        stmt = (
            pg_insert(EmailMessage)
            .values(values)
            .on_conflict_do_nothing(index_elements=["account_id", "uid"])
            .returning(EmailMessage.id)
        )
        try:
            result = self._s.execute(stmt)
            # 驱动对 ON CONFLICT 的 rowcount 不可靠（psycopg v3 返回 -1），
            # 以 RETURNING 实际返回的行数作为插入计数，跨驱动稳健
            return len(result.fetchall())
        except Exception as exc:
            raise RuntimeError(f"failed to bulk insert {len(messages)} emails: {exc}") from exc
