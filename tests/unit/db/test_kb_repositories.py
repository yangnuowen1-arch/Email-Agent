"""db.repositories 知识库仓储单元测试：CRUD、幂等语义与相似度语句封装。

运行环境为内存 SQLite（kb 两表经 with_variant 可建表）；涉及真向量
``<=>`` 计算的行为由 tests/integration/test_kb_repository.py 连 PG 验证。
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.db import Base, KbChunk, KbDocument
from app.db.repositories import (
    KbChunkRepository,
    KbDocumentRepository,
    _build_similarity_stmt,
)
from app.schemas.knowledge import KB_EMBEDDING_DIMENSIONS


@pytest.fixture
async def sqlite_engine():
    """内存 SQLite：仅创建 kb 两表（邮件表与本题无关）。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[KbDocument.__table__, KbChunk.__table__])
        )
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(sqlite_engine):
    maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


def _make_doc(**overrides):
    fields = {
        "kb_type": "faq",
        "title": "产品与价格 FAQ",
        "source_key": "file:docs/faq-pricing-v1.md",
        "content_hash": "a" * 64,
    }
    return KbDocument(**{**fields, **overrides})


def _make_chunk(document_id: int, **overrides):
    fields = {
        "document_id": document_id,
        "kb_type": "faq",
        "chunk_index": 0,
        "content": f"chunk of doc {document_id}",
        "embedding": [0.1] * KB_EMBEDDING_DIMENSIONS,
        "embedding_model": "seed-dummy-1536",
    }
    return KbChunk(**{**fields, **overrides})


# ---------------------------------------------------------------------------
# KbDocumentRepository
# ---------------------------------------------------------------------------


class TestKbDocumentRepository:
    async def test_create_returns_with_id(self, session):
        repo = KbDocumentRepository(session)
        doc = await repo.create_kb_document(_make_doc())

        assert doc.id is not None
        assert doc.created_at is not None
        assert doc.updated_at is not None

    async def test_get_by_id(self, session):
        repo = KbDocumentRepository(session)
        doc = await repo.create_kb_document(_make_doc())

        assert (await repo.get_kb_document_by_id(doc.id)).id == doc.id
        assert await repo.get_kb_document_by_id(99999) is None

    async def test_get_by_source_key(self, session):
        repo = KbDocumentRepository(session)
        await repo.create_kb_document(_make_doc(source_key="file:x.md"))

        found = await repo.get_kb_document_by_source_key("file:x.md")
        assert found is not None and found.source_key == "file:x.md"
        assert await repo.get_kb_document_by_source_key("file:missing.md") is None

    async def test_same_source_key_rejected(self, session):
        """幂等语义的底线：UNIQUE(source_key) 让同来源重入库必然失败。"""
        repo = KbDocumentRepository(session)
        await repo.create_kb_document(_make_doc(source_key="file:same.md"))

        with pytest.raises(IntegrityError):
            await repo.create_kb_document(_make_doc(source_key="file:same.md"))

    async def test_list_by_kb_type_with_active_only(self, session):
        repo = KbDocumentRepository(session)
        await repo.create_kb_document(_make_doc(source_key="file:a.md", status="active"))
        await repo.create_kb_document(_make_doc(source_key="file:b.md", status="archived"))
        await repo.create_kb_document(_make_doc(kb_type="compliance", source_key="text:c.md"))

        docs = await repo.list_kb_document_by_kb_type("faq")
        assert {d.source_key for d in docs} == {"file:a.md", "file:b.md"}

        active = await repo.list_kb_document_by_kb_type("faq", active_only=True)
        assert [d.source_key for d in active] == ["file:a.md"]

    async def test_list_by_kb_type_rejects_invalid_type(self, session):
        repo = KbDocumentRepository(session)
        with pytest.raises(ValueError, match="kb_type"):
            await repo.list_kb_document_by_kb_type("blog")

    async def test_update_partial_fields(self, session):
        repo = KbDocumentRepository(session)
        doc = await repo.create_kb_document(_make_doc())

        assert await repo.update_kb_document_by_id(doc.id, status="archived")
        refreshed = await repo.get_kb_document_by_id(doc.id)
        assert refreshed.status == "archived"
        assert refreshed.title == "产品与价格 FAQ"  # 未传字段不受影响

        assert await repo.update_kb_document_by_id(doc.id, content_hash="b" * 64)
        refreshed = await repo.get_kb_document_by_id(doc.id)
        assert refreshed.content_hash == "b" * 64

    async def test_update_returns_false_when_missing(self, session):
        repo = KbDocumentRepository(session)
        assert not await repo.update_kb_document_by_id(99999, status="archived")

    async def test_update_rejects_invalid_status(self, session):
        repo = KbDocumentRepository(session)
        doc = await repo.create_kb_document(_make_doc())
        with pytest.raises(ValueError, match="status"):
            await repo.update_kb_document_by_id(doc.id, status="gone")

    async def test_delete_by_id(self, session):
        repo = KbDocumentRepository(session)
        doc = await repo.create_kb_document(_make_doc())

        assert await repo.delete_kb_document_by_id(doc.id)
        assert await repo.get_kb_document_by_id(doc.id) is None
        assert not await repo.delete_kb_document_by_id(doc.id)


# ---------------------------------------------------------------------------
# KbChunkRepository
# ---------------------------------------------------------------------------


class TestKbChunkRepository:
    async def test_create_returns_with_id(self, session):
        doc = await KbDocumentRepository(session).create_kb_document(_make_doc())
        repo = KbChunkRepository(session)

        chunk = await repo.create_kb_chunk(_make_chunk(doc.id))
        assert chunk.id is not None
        assert (await repo.get_kb_chunk_by_id(chunk.id)).content == "chunk of doc 1"

    async def test_create_rejects_foreign_entity(self, session):
        repo = KbChunkRepository(session)
        with pytest.raises(TypeError, match="KbChunk"):
            await repo.create_kb_chunk(_make_doc())  # type: ignore[arg-type]

    async def test_bulk_create_preserves_order_and_ids(self, session):
        doc = await KbDocumentRepository(session).create_kb_document(_make_doc())
        repo = KbChunkRepository(session)

        chunks = [_make_chunk(doc.id, chunk_index=i, content=f"part {i}") for i in range(3)]
        saved = await repo.bulk_create_kb_chunk(chunks)

        assert all(c.id is not None for c in saved)
        assert [c.chunk_index for c in saved] == [0, 1, 2]

    async def test_list_by_document_id_orders_by_chunk_index(self, session):
        doc = await KbDocumentRepository(session).create_kb_document(_make_doc())
        repo = KbChunkRepository(session)
        # 乱序插入，读取应按 chunk_index 升序（还原上下文的顺序）
        await repo.bulk_create_kb_chunk([_make_chunk(doc.id, chunk_index=i) for i in (2, 0, 1)])

        chunks = await repo.list_kb_chunk_by_document_id(doc.id)
        assert [c.chunk_index for c in chunks] == [0, 1, 2]

    async def test_duplicate_index_within_document_rejected(self, session):
        doc = await KbDocumentRepository(session).create_kb_document(_make_doc())
        repo = KbChunkRepository(session)
        await repo.create_kb_chunk(_make_chunk(doc.id, chunk_index=0))

        with pytest.raises(IntegrityError):
            await repo.create_kb_chunk(_make_chunk(doc.id, chunk_index=0))

    async def test_delete_by_document_id_only_touches_target(self, session):
        docs = KbDocumentRepository(session)
        doc_a = await docs.create_kb_document(_make_doc(source_key="file:a.md"))
        doc_b = await docs.create_kb_document(_make_doc(source_key="file:b.md"))
        repo = KbChunkRepository(session)
        await repo.bulk_create_kb_chunk(
            [_make_chunk(doc_a.id, chunk_index=i) for i in range(3)]
            + [_make_chunk(doc_b.id, chunk_index=0)]
        )

        assert await repo.delete_kb_chunk_by_document_id(doc_a.id) == 3

        remaining = await repo.list_kb_chunk_by_document_id(doc_b.id)
        assert [c.chunk_index for c in remaining] == [0]
        assert await repo.list_kb_chunk_by_document_id(doc_a.id) == []


# ---------------------------------------------------------------------------
# list_kb_chunk_by_similarity：参数校验 + 语句封装（sqlite 冒烟）
# ---------------------------------------------------------------------------


class TestSimilarityStatement:
    def test_where_filters_type_active_and_model(self):
        """类型过滤 + 仅 active 文档参与 + 同模型匹配，三个条件都在 WHERE 中。"""
        stmt = _build_similarity_stmt(
            "faq", [0.1] * KB_EMBEDDING_DIMENSIONS, top_k=5, embedding_model="m1"
        )
        where_sql = str(stmt.whereclause.compile(dialect=sqlite.dialect()))

        assert "kb_chunks.kb_type = ?" in where_sql
        assert "kb_documents.status = ?" in where_sql
        assert "kb_chunks.embedding_model = ?" in where_sql

    def test_order_by_cosine_distance_and_limit(self):
        stmt = _build_similarity_stmt("faq", [0.1] * KB_EMBEDDING_DIMENSIONS, top_k=5)
        order_sql = str(stmt._order_by_clauses[0].compile(dialect=sqlite.dialect()))

        assert "<=>" in order_sql  # pgvector 余弦距离算子
        assert stmt._limit_clause.value == 5

    def test_model_filter_omitted_when_not_provided(self):
        stmt = _build_similarity_stmt("sop", [0.1] * KB_EMBEDDING_DIMENSIONS, top_k=3)
        where_sql = str(stmt.whereclause.compile(dialect=sqlite.dialect()))
        assert "embedding_model" not in where_sql


class TestSimilarityValidation:
    async def test_rejects_invalid_kb_type(self, session):
        repo = KbChunkRepository(session)
        with pytest.raises(ValueError, match="kb_type"):
            await repo.list_kb_chunk_by_similarity("blog", [0.1] * KB_EMBEDDING_DIMENSIONS)

    async def test_rejects_non_positive_top_k(self, session):
        repo = KbChunkRepository(session)
        with pytest.raises(ValueError, match="top_k"):
            await repo.list_kb_chunk_by_similarity("faq", [0.1] * KB_EMBEDDING_DIMENSIONS, top_k=0)

    async def test_rejects_wrong_embedding_dimension(self, session):
        """维度不符在进入 SQL 前即失败：不同维度向量不可比，属调用方错误。"""
        repo = KbChunkRepository(session)
        with pytest.raises(ValueError, match="query_embedding"):
            await repo.list_kb_chunk_by_similarity("faq", [0.1] * 768)
