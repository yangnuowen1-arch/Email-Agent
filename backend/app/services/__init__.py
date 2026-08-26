# 确定性领域逻辑层对外导出：解析纯函数 + 同步编排
from app.services.parsing import parse_email  # RFC822 字节 → ParsedEmail
from app.services.sync import SyncResult, sync_account, sync_all  # 同步结果与核心编排函数

__all__ = ["SyncResult", "parse_email", "sync_account", "sync_all"]
