# 配置模块对外导出；Container 需显式从 app.core.container 导入，避免
# 仅导入 settings 时初始化外部基础设施依赖。
from .settings import AppConfig

__all__ = ["AppConfig"]
