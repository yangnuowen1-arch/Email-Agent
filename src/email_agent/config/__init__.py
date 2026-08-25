# 配置模块对外导出，环境变量加载的唯一入口
from email_agent.config.settings import AppConfig  # 全局配置对象

__all__ = ["AppConfig"]
