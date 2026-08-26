"""显式依赖容器：持有配置与 Database，统一管理运行期对象的生命周期。"""

from __future__ import annotations

import structlog

from app.agent import EmailAgent
from app.core.ingest import IngestCoordinator
from app.core.settings import AppConfig, get_llm_config
from app.db.engine import Database, build_database
from app.observability import configure_logging
from app.services.email import EmailService


class Container:
    """全局对象容器：构造时装配 Database、EmailService 与 IngestCoordinator，生命周期统一接管。

    引擎/会话工厂不设模块级全局单例；业务层经 ``container.database.session()``
    获取事务性会话。``EmailService`` 仅负责读邮件，``IngestCoordinator`` 负责读+落库的编排。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

        # 结构化日志：构造时一次性配置，后续所有 logging/structlog 调用输出 JSON
        configure_logging(self._config.log_level)
        self._logger = structlog.get_logger("email-agent")

        # Database 仅在构造时装配一次，连接到首次使用才真正建立
        self._database = build_database(config)

        # EmailService
        self._email = EmailService()

        # IngestCoordinator 持有 DB 操作权，编排“读取→落库”
        self._coordinator = IngestCoordinator(
            database=self._database,
            email_reader=self._email,
            config=self._config,
        )

        # EmailAgent 延迟到首次访问时构建，避免无 LLM_API_KEY 时影响 ingest 等既有流程
        self._agent: EmailAgent | None = None

    @property
    def config(self) -> AppConfig:
        """返回全局配置，CLI 与业务层据此读取环境变量驱动的参数。"""
        return self._config

    @property
    def database(self) -> Database:
        """返回进程内唯一的 Database 门面，异步上下文中经它开事务。"""
        return self._database

    @property
    def email(self) -> EmailService:
        """返回邮件读取服务实例。"""
        return self._email

    @property
    def coordinator(self) -> IngestCoordinator:
        """返回邮件同步编排器实例。"""
        return self._coordinator

    @property
    def agent(self) -> EmailAgent:
        """返回邮件智能体门面；首次访问时按 LLM 配置构建并缓存。"""
        if self._agent is None:
            self._agent = EmailAgent(get_llm_config())
        return self._agent

    @property
    def logger(self):
        """返回已配置的结构化日志记录器，cli 与业务层经它输出 JSON 日志。"""
        return self._logger

    async def close_all(self) -> None:
        """释放 Database 持有的全部连接；同步入口（如 CLI）负责用 asyncio.run 桥接。"""
        await self._database.dispose()
