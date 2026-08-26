from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from app.core.settings import AppConfig

# 全局引擎单例，多线程间共享
_engine: Engine | None = None
# 保护引擎初始化/关闭的互斥锁，避免并发创建多个引擎
_lock = threading.Lock()


def init_engine(config: AppConfig) -> Engine:
    """初始化全局 SQLAlchemy 引擎（自带连接池，使用 psycopg v3 驱动）。

    连接串须为 ``postgresql+psycopg://`` 形式（见 ``.env.example``）。
    """
    global _engine
    # 加锁保证多线程同时调用 init_engine 时只有一个能创建引擎
    with _lock:
        # 如果已存在旧引擎，先释放其连接，避免泄漏
        if _engine is not None:
            _engine.dispose()
            _engine = None
        try:
            # 引擎内置连接池：pool_size 对应原 maxconn 上限，
            # max_overflow=0 使连接数硬上限等于 pool_size（与原 ThreadedConnectionPool 语义一致）
            # pool_pre_ping 在借出连接时做一次健康检查，自动丢弃失效连接
            _engine = create_engine(
                config.database_url,
                pool_size=config.db_pool_max_size,
                max_overflow=0,
                pool_pre_ping=True,
                future=True,
            )
        except Exception as exc:  # noqa: BLE001
            # 初始化失败时包装为 RuntimeError，带上原始异常便于排查连接串/网络问题
            raise RuntimeError(f"failed to init DB engine: {exc}") from exc
        return _engine


def get_engine() -> Engine:
    """获取已初始化的引擎，未初始化则报错。"""
    if _engine is None:
        # 防御性检查，避免在未调用 init_engine 前就尝试获取连接
        msg = "DB engine not initialized; call init_engine(config) first"
        raise RuntimeError(msg)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """获取绑定到引擎的会话工厂，调用即得到一个新 Session（独占连接）。"""
    # expire_on_commit=False：提交后已加载的对象属性仍可读，避免跨线程传值时意外触发懒加载
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def close_engine() -> None:
    """释放引擎及其所有连接，通常在程序退出时调用。"""
    global _engine
    with _lock:
        if _engine is not None:
            try:
                _engine.dispose()
            finally:
                # 无论关闭是否成功，都将全局引用置空，避免后续误用已关闭的引擎
                _engine = None
