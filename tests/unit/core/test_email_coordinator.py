"""EmailCoordinator 合规红线注入的最小单元测试：全量读取、归档过滤与失败降级。

仅覆盖 _load_compliance_rules（kb 两表可在 SQLite 建表）；analyze_email 全流程
依赖 PG 方言表与真实 LLM，由集成场景覆盖。
"""

from __future__ import annotations

import pytest
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.email_coordinator import EmailCoordinator
from app.core.settings import AppConfig, LLMConfig
from app.db.db import Base, KbChunk, KbDocument
from app.db.engine import Database
from app.db.repositories import KbChunkRepository, KbDocumentRepository
from app.schemas.knowledge import KB_EMBEDDING_DIMENSIONS


def _make_coordinator(database) -> EmailCoordinator:
    config = AppConfig(database_url="unused", llm=LLMConfig(llm_api_key="test-key-123"))
    return EmailCoordinator(config, database, structlog.get_logger())


def _make_rule_doc(source_key: str, *, status: str = "active") -> KbDocument:
    return KbDocument(
        kb_type="compliance",
        title="回复红线规则",
        source_key=source_key,
        content_hash=source_key * 10,
        status=status,
    )


def _make_rule_chunk(document_id: int, chunk_index: int, content: str) -> KbChunk:
    return KbChunk(
        document_id=document_id,
        kb_type="compliance",
        chunk_index=chunk_index,
        content=content,
        embedding=[0.1] * KB_EMBEDDING_DIMENSIONS,
        embedding_model="fake-embed",
    )


@pytest.fixture
async def kb_database():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[KbDocument.__table__, KbChunk.__table__])
        )
    yield Database(engine, sessions=async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


async def test_load_compliance_rules_returns_active_in_order(kb_database):
    coordinator = _make_coordinator(kb_database)
    async with kb_database.session() as session:
        docs = KbDocumentRepository(session)
        doc = await docs.create_kb_document(_make_rule_doc("text:redline-v1"))
        chunks = KbChunkRepository(session)
        await chunks.bulk_create_kb_chunk(
            [
                _make_rule_chunk(doc.id, 1, "禁止承诺退款到账时间"),
                _make_rule_chunk(doc.id, 0, "不得承诺最低价"),
            ]
        )

    rules = await coordinator._load_compliance_rules()

    assert rules == ["不得承诺最低价", "禁止承诺退款到账时间"]


async def test_load_compliance_rules_excludes_archived_and_other_types(kb_database):
    coordinator = _make_coordinator(kb_database)
    async with kb_database.session() as session:
        docs = KbDocumentRepository(session)
        doc = await docs.create_kb_document(_make_rule_doc("text:redline-v1"))
        archived = await docs.create_kb_document(
            _make_rule_doc("text:redline-old", status="archived")
        )
        chunks = KbChunkRepository(session)
        await chunks.bulk_create_kb_chunk(
            [
                _make_rule_chunk(doc.id, 0, "现行红线"),
                _make_rule_chunk(archived.id, 0, "已归档红线"),
            ]
        )

    rules = await coordinator._load_compliance_rules()

    assert rules == ["现行红线"]


async def test_load_compliance_rules_empty_when_no_data(kb_database):
    coordinator = _make_coordinator(kb_database)
    assert await coordinator._load_compliance_rules() == []


async def test_load_compliance_rules_degrades_on_db_error():
    """读红线失败（如知识库未建表）→ 降级为空列表，不拖垮分析主链。"""

    class _BrokenDatabase:
        def session(self):
            raise RuntimeError("db down")

    coordinator = _make_coordinator(_BrokenDatabase())

    assert await coordinator._load_compliance_rules() == []
