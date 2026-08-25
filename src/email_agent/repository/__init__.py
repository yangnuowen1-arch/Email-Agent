# 数据访问层对外导出，唯一允许写 SQL 的地方（基于 Session 的薄封装）
from email_agent.repository.email_accounts import AccountStore
from email_agent.repository.emails import EmailStore

__all__ = ["AccountStore", "EmailStore"]
