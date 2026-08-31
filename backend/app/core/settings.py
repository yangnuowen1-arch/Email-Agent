from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


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
    """应用全局配置，完全由环境变量/.env 驱动。"""

    # 必填：PostgreSQL 连接串，格式 postgresql+psycopg://user:password@host:port/dbname
    database_url: str
    # 连接池最小/最大连接数，I/O 密集场景下可适当调大
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    # 账号级并发数，对应 ThreadPoolExecutor 的 max_workers
    sync_max_workers: int = 5
    # 单账号同步超时时间（秒），超时后记为失败并隔离，不影响其他账号
    sync_timeout_seconds: int = 60
    # 日志级别，控制 JSON 结构化日志的输出粒度
    log_level: str = "INFO"
    # Gemini Developer API 的密钥；未配置时仍可运行纯 IMAP 同步。
    gemini_api_key: str | None = None
    # 使用 Gemini 的裸模型 ID，不包含 "models/" 前缀。
    gemini_model: str = "gemini-2.5-flash"

    def __post_init__(self) -> None:
        """初始化后校验：确保所有配置项都在合法范围内。"""
        # 数据库 URL 不能为空，否则后续所有 DB 操作都会失败，需尽早报错
        if not self.database_url or not self.database_url.strip():
            raise ValueError("DATABASE_URL is required but missing or empty")
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
        # 并发数和超时时间也必须为正整数
        if self.sync_max_workers < 1:
            msg = f"SYNC_MAX_WORKERS must be >=1, got {self.sync_max_workers!r}"
            raise ValueError(msg)
        if self.sync_timeout_seconds < 1:
            msg = f"SYNC_TIMEOUT_SECONDS must be >=1, got {self.sync_timeout_seconds!r}"
            raise ValueError(msg)
        if not isinstance(self.gemini_model, str) or not self.gemini_model.strip():
            raise ValueError("GEMINI_MODEL must not be empty")

    @classmethod
    def from_env(cls) -> AppConfig:
        """从环境变量和 .env 文件加载配置。"""
        # 加载 .env 文件，override=False 表示已有的环境变量优先，保持容器/CI 注入的优先级
        load_dotenv(override=False)

        # 读取必填的数据库连接串，缺失时给出明确提示，引导用户查看 .env.example
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError(
                "DATABASE_URL is required but missing or empty; "
                "set it in environment or .env file (see .env.example)"
            )

        # 读取可选的整型配置，均通过 _parse_int 解析，非法值会抛出带变量名的错误
        db_pool_min_size = _parse_int("DB_POOL_MIN_SIZE", os.getenv("DB_POOL_MIN_SIZE"), 1)
        db_pool_max_size = _parse_int("DB_POOL_MAX_SIZE", os.getenv("DB_POOL_MAX_SIZE"), 10)
        sync_max_workers = _parse_int("SYNC_MAX_WORKERS", os.getenv("SYNC_MAX_WORKERS"), 5)
        sync_timeout_seconds = _parse_int(
            "SYNC_TIMEOUT_SECONDS", os.getenv("SYNC_TIMEOUT_SECONDS"), 60
        )

        # 日志级别统一转大写，空值回退到 INFO，避免大小写敏感导致配置不生效
        log_level = (os.getenv("LOG_LEVEL", "INFO") or "INFO").strip().upper() or "INFO"
        gemini_api_key = (os.getenv("GEMINI_API_KEY") or "").strip() or None
        gemini_model = (
            (os.getenv("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash").strip()
            or "gemini-2.5-flash"
        )

        return cls(
            database_url=database_url,
            db_pool_min_size=db_pool_min_size,
            db_pool_max_size=db_pool_max_size,
            sync_max_workers=sync_max_workers,
            sync_timeout_seconds=sync_timeout_seconds,
            log_level=log_level,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
        )
