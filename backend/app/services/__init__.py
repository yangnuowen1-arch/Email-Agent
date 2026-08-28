# 确定性领域逻辑层对外导出：解析纯函数 + 邮件读取服务
from app.services.email import EmailService  # 邮件读取服务
from app.services.parsing import parse_email  # RFC822 字节 → ParsedEmail

__all__ = ["EmailService", "parse_email"]
