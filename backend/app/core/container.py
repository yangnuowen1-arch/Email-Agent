"""显式依赖容器：持有配置与 Database，统一管理运行期对象的生命周期。"""

from __future__ import annotations

import structlog

from app.core.email_coordinator import EmailCoordinator
from app.core.listener import EmailListener
from app.core.settings import AppConfig
from app.db.engine import Database, build_database
from app.observability import configure_logging
from app.providers.email.base import AccountConfig, MailClient
from app.providers.email.factory import create_client
from app.services.email import EmailService


class Container:
    """全局对象容器：构造时装配 Database、EmailService 与 EmailListener，生命周期统一接管。

    引擎/会话工厂不设模块级全局单例；业务层经 ``container.database.session()``
    获取事务性会话。``EmailService`` 仅负责阻塞接收与解析邮件，
    ``EmailListener`` 负责一账号一线程的长驻监听编排与落库。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

        configure_logging(self._config.log_level)
        self._logger = structlog.get_logger("email-agent")

        if self._config.database_url is None:
            raise AttributeError("Database URL is required")
        self._database = build_database(config)

        self._email = EmailService(client_factory=self._make_client_factory())

        self._listener = EmailListener(
            database=self._database,
            email_service=self._email,
            config=self._config,
            logger=self._logger,
        )

        self._email_coordinator = EmailCoordinator(
            config=self._config,
            database=self._database,
            logger=self._logger,
        )

    def _make_client_factory(self):
        """组合根负责把监听调优配置落到具体客户端构造参数上。"""

        def factory(account: AccountConfig) -> MailClient:
            return create_client(
                account,
                idle_ping_interval=float(self._config.listen_idle_ping_seconds),
                backoff_initial_seconds=float(self._config.listen_backoff_initial_seconds),
                backoff_max_seconds=float(self._config.listen_backoff_max_seconds),
            )

        return factory

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
        """返回邮件接收服务实例。"""
        return self._email

    @property
    def listener(self) -> EmailListener:
        """返回邮件监听器实例。"""
        return self._listener

    @property
    def email_coordinator(self) -> EmailCoordinator:
        """返回邮件智能体编排器。"""
        return self._email_coordinator

    @property
    def logger(self):
        """返回已配置的结构化日志记录器，cli 与业务层经它输出 JSON 日志。"""
        return self._logger

    async def close_all(self) -> None:
        """释放容器持有的全部运行期对象；同步入口（如 CLI）负责用 asyncio.run 桥接。"""
        if self._listener is not None:
            await self._listener.stop()

        if self._database is not None:
            await self._database.dispose()
