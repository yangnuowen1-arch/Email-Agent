# 邮件解析层对外导出，纯函数，无外部依赖
from email_agent.parsing.parser import parse_email  # RFC822 字节 → EmailMessage

__all__ = ["parse_email"]
