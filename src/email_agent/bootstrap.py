from __future__ import annotations

from email_agent.config.settings import AppConfig
from email_agent.db.engine import close_engine, init_engine
from email_agent.db.engine import get_session_factory as _get_session_factory


class AppContext:
    """组合根：装配配置、引擎与 repository，是 CLI 与底层之间唯一的粘合点。

    引擎与 AppContext 只创建一次；``session_factory`` 按调用返回新的 Session（独占连接，
    即一个事务单元）。并发同步时每个账号线程各持独立 Session 与连接，保证线程安全、
    事务隔离与失败隔离；同一账号内的多表写操作共享该 Session，由一次 commit 原子提交。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        # 引擎生命周期由组合根接管，仅初始化一次
        init_engine(config)

    @property
    def session_factory(self):  # noqa: ANN201
        """返回绑定到引擎的会话工厂，调用即得到一个新 Session。"""
        return _get_session_factory()

    def close_all(self) -> None:
        """关闭引擎并释放所有连接，程序退出时调用。"""
        close_engine()
