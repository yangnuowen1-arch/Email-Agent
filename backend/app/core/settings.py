from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    """LLM 智能体运行时配置（OpenAI 兼容网关），由环境变量/.env 驱动。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "email-agent"

    log_level: str = "INFO"

    # LLM 智能体运行配置（OpenAI 兼容网关）
    llm_provider: str = "openai"
    llm_model: str = "mimo-v2.5"
    llm_base_url: str = "https://opencode.ai/zen/go/v1"
    llm_api_key: str | None = None
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    llm_timeout_seconds: int = 320

    # 视觉模型名（识别图片附件用，OpenAI 兼容网关）；未配置则跳过图片识别
    llm_vision_model: str | None = None

    # embedding 模型名（知识库向量化用，同网关 /embeddings 端点）
    # 未配置时 rag 相关功能抛 LLMConfigurationError，邮件主链路不受影响
    llm_embedding_model: str | None = None
    # 仅当网关 embedding 模型支持 dimensions 参数时设置（如 1536）；
    # 不设则由 rag 层按 KB_EMBEDDING_DIMENSIONS 校验实际返回维度兜底
    llm_embedding_dimensions: int | None = None


class CosConfig(BaseSettings):
    """腾讯云 COS 对象存储配置，由环境变量/.env 驱动。

    未配置（缺任一项）时附件链路降级：仅落元数据，不上传、不提取内容。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cos_secret_id: str | None = None
    cos_secret_key: str | None = None
    # 桶名，如 email-agent-1250000000
    cos_bucket: str | None = None
    # 地域，如 ap-guangzhou
    cos_region: str | None = None


def _parse_int(name: str, raw: str | None, default: int) -> int:
    """解析环境变量中的整数字符串，带默认值和友好报错。"""
    # 变量未设置或为空白字符串时，直接返回默认值，避免后续 int 转换报错
    if raw is None or raw.strip() == "":
        return default
    try:
        # 去除首尾空白后转为 int，支持 " 10 " 这类带空格的输入
        return int(raw.strip())
    except ValueError as exc:
        # 转换失败时抛出带变量名的错误，方便用户定位是哪个环境变量写错了
        msg = f"{name} must be int, got {raw!r}"
        raise ValueError(msg) from exc


@dataclass(frozen=True, kw_only=True)
class AppConfig:
    """应用全局配置，完全由环境变量/.env 驱动。

    ``llm`` 为内嵌的 LLM 运行时配置，由 ``from_env`` 在加载时一并实例化；
    agent 与 llm 网关的解耦由此收敛到单一配置来源，不再依赖独立的全局工厂。
    """

    # 必填（默认）：PostgreSQL 连接串，格式 postgresql+psycopg://user:password@host:port/dbname
    database_url: str
    # 连接池最小/最大连接数，I/O 密集场景下可适当调大
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    # IMAP IDLE 重发/健康检查周期（秒）：每次醒来做一次轻量搜索兜底丢事件
    listen_idle_ping_seconds: int = 60
    # 断线重连退避起点与上限（秒），指数递增封顶
    listen_backoff_initial_seconds: int = 1
    listen_backoff_max_seconds: int = 60
    # 单个工具调用的超时时间（秒），由 tools/registry 在装配时套用到每个工具
    agent_tool_timeout_seconds: int = 30
    # 日志级别，控制 JSON 结构化日志的输出粒度
    log_level: str = "INFO"
    # 内嵌 LLM 运行时配置；缺省按环境变量实例化一份
    llm: LLMConfig = field(default_factory=LLMConfig)
    # 腾讯云 COS 配置；附件字节上 COS，DB 只存对象引用
    cos: CosConfig = field(default_factory=CosConfig)

    def __post_init__(self) -> None:
        """初始化后校验：确保所有配置项都在合法范围内。"""
        # 连接池大小必须为正整数
        if self.db_pool_min_size < 1:
            msg = f"DB_POOL_MIN_SIZE must be >=1, got {self.db_pool_min_size!r}"
            raise ValueError(msg)
        if self.db_pool_max_size < 1:
            msg = f"DB_POOL_MAX_SIZE must be >=1, got {self.db_pool_max_size!r}"
            raise ValueError(msg)
        # 最小连接数不能超过最大连接数，否则连接池初始化会报错
        if self.db_pool_min_size > self.db_pool_max_size:
            msg = (
                f"DB_POOL_MIN_SIZE ({self.db_pool_min_size}) "
                f"must be <= DB_POOL_MAX_SIZE ({self.db_pool_max_size})"
            )
            raise ValueError(msg)
        # 监听调优参数必须为正整数，退避起点不能超过上限
        if self.listen_idle_ping_seconds < 1:
            msg = f"LISTEN_IDLE_PING_SECONDS must be >=1, got {self.listen_idle_ping_seconds!r}"
            raise ValueError(msg)
        if self.listen_backoff_initial_seconds < 1:
            msg = (
                f"LISTEN_BACKOFF_INITIAL_SECONDS must be >=1, "
                f"got {self.listen_backoff_initial_seconds!r}"
            )
            raise ValueError(msg)
        if self.listen_backoff_max_seconds < self.listen_backoff_initial_seconds:
            msg = (
                f"LISTEN_BACKOFF_MAX_SECONDS ({self.listen_backoff_max_seconds}) must be >= "
                f"LISTEN_BACKOFF_INITIAL_SECONDS ({self.listen_backoff_initial_seconds})"
            )
            raise ValueError(msg)

    @classmethod
    def from_env(cls, require_database: bool = True) -> AppConfig:
        """从环境变量和 .env 文件加载配置。

        ``require_database`` 为 True 时强制要求 ``DATABASE_URL``（listen 等需要落库
        的流程）；agent 等无需数据库的流程可传 False，从而不依赖数据库即可启动。
        """
        # 加载 .env 文件，override=False 表示已有的环境变量优先，保持容器/CI 注入的优先级
        load_dotenv(override=False)

        # 读取可选的环境变量；DATABASE_URL 是否必填由 require_database 决定
        database_url = (os.getenv("DATABASE_URL") or "").strip()
        if require_database and not database_url:
            raise ValueError(
                "DATABASE_URL is required but missing or empty; "
                "set it in environment or .env file (see .env.example)"
            )

        # 读取可选的整型配置，均通过 _parse_int 解析，非法值会抛出带变量名的错误
        db_pool_min_size = _parse_int("DB_POOL_MIN_SIZE", os.getenv("DB_POOL_MIN_SIZE"), 1)
        db_pool_max_size = _parse_int("DB_POOL_MAX_SIZE", os.getenv("DB_POOL_MAX_SIZE"), 10)
        listen_idle_ping_seconds = _parse_int(
            "LISTEN_IDLE_PING_SECONDS", os.getenv("LISTEN_IDLE_PING_SECONDS"), 60
        )
        listen_backoff_initial_seconds = _parse_int(
            "LISTEN_BACKOFF_INITIAL_SECONDS", os.getenv("LISTEN_BACKOFF_INITIAL_SECONDS"), 1
        )
        listen_backoff_max_seconds = _parse_int(
            "LISTEN_BACKOFF_MAX_SECONDS", os.getenv("LISTEN_BACKOFF_MAX_SECONDS"), 60
        )

        # 日志级别统一转大写，空值回退到 INFO，避免大小写敏感导致配置不生效
        log_level = (os.getenv("LOG_LEVEL", "INFO") or "INFO").strip().upper() or "INFO"

        return cls(
            database_url=database_url,
            db_pool_min_size=db_pool_min_size,
            db_pool_max_size=db_pool_max_size,
            listen_idle_ping_seconds=listen_idle_ping_seconds,
            listen_backoff_initial_seconds=listen_backoff_initial_seconds,
            listen_backoff_max_seconds=listen_backoff_max_seconds,
            log_level=log_level,
        )
