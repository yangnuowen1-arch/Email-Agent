from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""

    # 统一元数据，便于集中管理表结构与迁移
    metadata = MetaData()
