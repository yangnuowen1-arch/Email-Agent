# 领域模型兼 ORM 实体对外导出，所有层通过此契约传递数据
from email_agent.models.account import Account  # 邮箱账号模型，对应 email_accounts 表
from email_agent.models.message import EmailMessage  # 已解析邮件模型，对应 emails 表

__all__ = ["Account", "EmailMessage"]
