"""显式依赖容器：持有配置与 Database，统一管理运行期对象的生命周期。"""

from __future__ import annotations

import structlog

from app.core.email_coordinator import EmailCoordinator
from app.core.settings import AppConfig
from app.core.sync import EmailSynchronizer
from app.db.engine import Database, build_database
from app.observability import configure_logging
from app.services.email import EmailService


class Container:
    """全局对象容器：构造时装配 Database、EmailService 与 EmailSynchronizer，生命周期统一接管。

    引擎/会话工厂不设模块级全局单例；业务层经 ``container.database.session()``
    获取事务性会话。``EmailService`` 仅负责读邮件，``EmailSynchronizer`` 负责读+落库的编排。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

        configure_logging(self._config.log_level)
        self._logger = structlog.get_logger("email-agent")

        if self._config.database_url is None:
            raise AttributeError("Database URL is required")
        self._database = build_database(config)

        self._email = EmailService()

        self._synchronizer = EmailSynchronizer(
            database=self._database,
            email_reader=self._email,
            config=self._config,
        )

        self._email_coordinator = EmailCoordinator(
            config=self._config,
            database=self._database,
            logger=self._logger,
        )

    @property
    def config(self) -> AppConfig:
        """返回全局配置，CLI 与业务层据此读取环境变量驱动的参数。"""
        return self._config

    @property
    def database(self) -> Database | None:
        """返回进程内唯一的 Database 门面，异步上下文中经它开事务。

        未提供 ``DATABASE_URL`` 时为 ``None``（如仅运行 agent），调用方需自行判断。
        """
        return self._database

    @property
    def email(self) -> EmailService:
        """返回邮件读取服务实例。"""
        return self._email

    @property
    def synchronizer(self) -> EmailSynchronizer:
        """返回邮件同步器实例。"""
        return self._synchronizer

    @property
    def email_coordinator(self) -> EmailCoordinator:
        """返回邮件智能体编排器。"""
        return self._email_coordinator

    @property
    def logger(self):
        """返回已配置的结构化日志记录器，cli 与业务层经它输出 JSON 日志。"""
        return self._logger

    async def close_all(self) -> None:
        """释放 Database 持有的全部连接；同步入口（如 CLI）负责用 asyncio.run 桥接。"""
        if self._database is not None:
            await self._database.dispose()
