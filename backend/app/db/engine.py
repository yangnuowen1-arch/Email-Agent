"""异步数据库门面：Database 对象装配与事务边界管理。

设计约束：引擎/会话工厂不设模块级全局单例，生命周期一律由
``app.core.container.Container`` 持有并在退出时统一释放。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# 导入模型包即把所有表注册进 Base.metadata，create_all 才能建全
import app.db.db  # noqa: F401
from app.core.settings import AppConfig


@dataclass(slots=True)
class Database:
    """异步数据库门面：持有引擎与会话工厂，事务边界由 session() 独占管理。"""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """提供一个事务性会话：正常退出即提交，异常即回滚并透传。

        commit/rollback 只在这里发生；repository 层用 flush() 取主键，
        禁止手动提交，保证一个上下文就是一个原子事务单元。
        """
        async with self.sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """释放底层引擎的连接池；由容器 ``close_all`` 统一调用。"""
        await self.engine.dispose()


def build_database(config: AppConfig) -> Database:
    """按配置装配 Database。

    连接池沿用既有语义：pool_size 对应 DB_POOL_MAX_SIZE 硬上限
    （max_overflow=0），pool_pre_ping 借出前做健康检查丢弃失效连接。
    引擎创建不产生任何网络 I/O，连接在首次借出时才真正建立。
    """
    engine = create_async_engine(
        config.database_url,
        pool_size=config.db_pool_max_size,
        max_overflow=0,
        pool_pre_ping=True,
    )

    return Database(engine=engine, sessions=async_sessionmaker(engine, expire_on_commit=False))
