# 数据库引擎/会话工厂对外导出，统一从此模块导入
from email_agent.db.engine import (
    close_engine,  # 关闭引擎并释放所有连接，程序退出时调用
    get_engine,  # 获取已初始化的引擎
    get_session_factory,  # 获取绑定引擎的会话工厂
    init_engine,  # 初始化引擎（含连接池）
)

__all__ = [
    "close_engine",
    "get_engine",
    "get_session_factory",
    "init_engine",
]
