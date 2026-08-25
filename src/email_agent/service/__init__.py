# 业务编排层对外导出，负责单账号与并发调度
from email_agent.service.sync import SyncResult, sync_account, sync_all  # 同步结果与核心编排函数

__all__ = ["SyncResult", "sync_account", "sync_all"]
