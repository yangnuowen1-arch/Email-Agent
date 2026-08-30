"""db.db 知识库模型单元测试：表名、约束、默认值与白名单校验。"""

from __future__ import annotations

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects import sqlite

from app.db.db import KbChunk, KbDocument
from app.schemas.knowledge import KB_EMBEDDING_DIMENSIONS


def _make_doc(**overrides):
    fields = {
        "kb_type": "faq",
        "title": "产品与价格 FAQ",
        "source_key": "file:docs/faq-pricing-v1.md",
        "content_hash": "a" * 64,
    }
    return KbDocument(**{**fields, **overrides})


def _make_chunk(**overrides):
    fields = {
        "document_id": 1,
        "kb_type": "faq",
        "chunk_index": 0,
        "content": "标准版 299 元/月",
        "embedding": [0.1] * KB_EMBEDDING_DIMENSIONS,
        "embedding_model": "seed-dummy-1536",
    }
    return KbChunk(**{**fields, **overrides})


class TestTableMapping:
    def test_table_names(self):
        assert KbDocument.__tablename__ == "kb_documents"
        assert KbChunk.__tablename__ == "kb_chunks"

    def test_source_key_unique_constraint(self):
        cols = KbDocument.__table__.columns
        assert cols["source_key"].unique is True
        assert cols["source_key"].nullable is False

    def test_chunk_composite_unique_constraint(self):
        uniques = [
            {c.name for c in u.columns}
            for u in KbChunk.__table__.constraints
            if isinstance(u, UniqueConstraint)
        ]
        assert {"document_id", "chunk_index"} in uniques

    def test_embedding_is_fixed_dim_vector_on_pg(self):
        emb = KbChunk.__table__.columns["embedding"].type
        assert emb.dim == KB_EMBEDDING_DIMENSIONS
        assert "VECTOR(1536)" in str(emb)

    def test_embedding_degrades_to_json_on_sqlite(self):
        """with_variant 兼容：sqlite 下退化为 JSON 列，PG 上仍是真向量列。"""
        emb = KbChunk.__table__.columns["embedding"].type
        impl = emb.dialect_impl(sqlite.dialect())
        assert type(impl).__name__ == "_SQliteJson"

    def test_meta_maps_to_metadata_column(self):
        """metadata 是 SQLAlchemy 保留属性名：属性名 meta，DB 列名 metadata。"""
        assert "metadata" in KbChunk.__table__.columns
        assert "meta" not in KbChunk.__table__.columns


class TestKbDocumentValidation:
    def test_defaults(self):
        doc = _make_doc()
        assert doc.source_type == "text"
        assert doc.status == "active"

    def test_invalid_kb_type_rejected(self):
        with pytest.raises(ValueError, match="kb_type"):
            _make_doc(kb_type="news")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError, match="status"):
            _make_doc(status="deleted")

    def test_invalid_source_type_rejected(self):
        with pytest.raises(ValueError, match="source_type"):
            _make_doc(source_type="url")

    @pytest.mark.parametrize("field", ["title", "source_key", "content_hash"])
    def test_required_fields_non_empty(self, field):
        with pytest.raises(ValueError, match=field):
            _make_doc(**{field: ""})


class TestKbChunkValidation:
    def test_meta_defaults_to_empty_dict(self):
        chunk = _make_chunk()
        assert chunk.meta == {}

    def test_invalid_kb_type_rejected(self):
        with pytest.raises(ValueError, match="kb_type"):
            _make_chunk(kb_type="knowledge")

    def test_negative_chunk_index_rejected(self):
        with pytest.raises(ValueError, match="chunk_index"):
            _make_chunk(chunk_index=-1)

    def test_wrong_embedding_dimension_rejected(self):
        with pytest.raises(ValueError, match="embedding"):
            _make_chunk(embedding=[0.1] * 768)

    def test_non_numeric_embedding_rejected(self):
        with pytest.raises(ValueError, match="embedding"):
            _make_chunk(embedding=["x"] * KB_EMBEDDING_DIMENSIONS)

    def test_empty_content_rejected(self):
        with pytest.raises(ValueError, match="content"):
            _make_chunk(content="")


class TestSqliteRoundTrip:
    """with_variant 后 sqlite 可建表落库：保证单测覆盖真实 CRUD 路径。"""

    async def test_document_and_chunk_roundtrip(self, tmp_path):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        from app.db.db import Base

        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda c: Base.metadata.create_all(
                        c, tables=[KbDocument.__table__, KbChunk.__table__]
                    )
                )
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                doc = _make_doc()
                session.add(doc)
                await session.flush()

                chunk = _make_chunk(document_id=doc.id, meta={"tags": ["pricing"]})
                session.add(chunk)
                await session.flush()

                loaded = await session.get(KbChunk, chunk.id)
                assert loaded.meta == {"tags": ["pricing"]}
                assert len(loaded.embedding) == KB_EMBEDDING_DIMENSIONS
        finally:
            await engine.dispose()
