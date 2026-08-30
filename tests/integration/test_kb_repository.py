"""知识库仓储集成测试（真 PostgreSQL + pgvector）。

验证 sqlite 单测无法覆盖的真向量行为：1536 维向量真实写入读出、
``<=>`` 余弦距离排序、kb_type / status / embedding_model 过滤在 PG 上生效。

运行前提：
    psql "$DATABASE_URL" -f scripts/kb_schema.sql      # 建表

用法：
    DATABASE_URL="postgresql+psycopg://..." pytest tests/integration -q

数据隔离：本文件的所有行都写入后**不提交**（fixture 结束统一回滚），
不会在数据库留下任何测试数据，也不污染 scripts/kb_seed.sql 的种子数据。
"""

from __future__ import annotations

import os
import random

import pytest
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.db import KbChunk, KbDocument
from app.db.repositories import KbChunkRepository, KbDocumentRepository
from app.schemas.knowledge import KB_EMBEDDING_DIMENSIONS

load_dotenv(override=False)


def _unit_vector(ones: int) -> list[float]:
    """构造 1536 维受控向量：前 ones 位为 1，其余为 0（余弦距离可手算）。"""
    return [1.0] * ones + [0.0] * (KB_EMBEDDING_DIMENSIONS - ones)


def _random_vector(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [round(rng.uniform(-1.0, 1.0), 6) for _ in range(KB_EMBEDDING_DIMENSIONS)]


@pytest.fixture
async def pg_session():
    """真 PG 会话：未配置 DATABASE_URL 或未建表时跳过；结束后回滚保证零残留。"""
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("DATABASE_URL 未设置，跳过真 PG 向量集成测试")

    engine = create_async_engine(database_url, poolclass=NullPool)

    # 表存在性探测：未执行 scripts/kb_schema.sql 时给出明确指引而非报错
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM kb_documents LIMIT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"kb 表不存在（先执行 scripts/kb_schema.sql）：{type(exc).__name__}")

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()  # 测试数据零残留

    await engine.dispose()


async def _make_doc_with_chunks(
    session: AsyncSession,
    *,
    kb_type: str,
    status: str,
    embedding_model: str,
    embeddings: list[list[float]],
) -> int:
    """一文档多分块的测试夹具（flush 不 commit，随会话回滚）。"""
    doc_repo = KbDocumentRepository(session)
    doc = await doc_repo.create_kb_document(
        KbDocument(
            kb_type=kb_type,
            title="itest 文档",
            source_type="text",
            source_key=f"itest:{os.urandom(8).hex()}",
            content_hash="itest" * 16,
            status=status,
        )
    )
    chunk_repo = KbChunkRepository(session)
    await chunk_repo.bulk_create_kb_chunk(
        [
            KbChunk(
                document_id=doc.id,
                kb_type=kb_type,
                chunk_index=i,
                content=f"itest chunk {i}",
                embedding=vec,
                embedding_model=embedding_model,
            )
            for i, vec in enumerate(embeddings)
        ]
    )
    return doc.id


class TestVectorRoundTrip:
    async def test_1536_dim_vector_write_and_read(self, pg_session):
        seed_vec = _random_vector(42)
        doc_id = await _make_doc_with_chunks(
            pg_session,
            kb_type="faq",
            status="active",
            embedding_model="itest-model",
            embeddings=[seed_vec],
        )

        chunk = (await KbChunkRepository(pg_session).list_kb_chunk_by_document_id(doc_id))[0]

        # pgvector 内部 float4 存储：以 1e-5 容差比较
        assert len(chunk.embedding) == KB_EMBEDDING_DIMENSIONS
        assert chunk.embedding == pytest.approx(seed_vec, abs=1e-5)


class TestSimilarityOnPostgres:
    async def test_orders_by_cosine_distance(self, pg_session):
        """受控向量手算余弦距离：同向 0 < [1,1,0..] 的 1-1/√2 < [1,1,1,0..] 的 1-1/√3。"""
        e1, e2, e3 = _unit_vector(1), _unit_vector(2), _unit_vector(3)
        await _make_doc_with_chunks(
            pg_session,
            kb_type="faq",
            status="active",
            embedding_model="itest-model",
            embeddings=[e2, e1, e3],  # 故意乱序插入：chunk_index 0/1/2 ↔ e2/e1/e3
        )

        results = await KbChunkRepository(pg_session).list_kb_chunk_by_similarity(
            "faq", _unit_vector(1), top_k=3, embedding_model="itest-model"
        )

        assert [c.chunk_index for c, _ in results] == [1, 0, 2]
        distances = [d for _, d in results]
        assert distances[0] == pytest.approx(0.0, abs=1e-6)  # 与查询同向
        assert distances[1] == pytest.approx(1 - 2**-0.5, abs=1e-5)  # 夹角 45°
        assert distances[2] == pytest.approx(1 - 3**-0.5, abs=1e-5)  # 夹角 ≈54.7°
        assert distances == sorted(distances)

    async def test_kb_type_filter(self, pg_session):
        await _make_doc_with_chunks(
            pg_session,
            kb_type="faq",
            status="active",
            embedding_model="itest-model",
            embeddings=[_unit_vector(1)],
        )
        await _make_doc_with_chunks(
            pg_session,
            kb_type="compliance",
            status="active",
            embedding_model="itest-model",
            embeddings=[_unit_vector(1)],  # 与查询向量完全同向，但类型不同必须被过滤
        )

        results = await KbChunkRepository(pg_session).list_kb_chunk_by_similarity(
            "faq", _unit_vector(1), top_k=10, embedding_model="itest-model"
        )

        assert results, "至少命中 faq 自身分块"
        assert {c.kb_type for c, _ in results} == {"faq"}

    async def test_archived_document_excluded(self, pg_session):
        """归档文档的分块即使与查询向量完全同向也不得命中。"""
        await _make_doc_with_chunks(
            pg_session,
            kb_type="faq",
            status="active",
            embedding_model="itest-model",
            embeddings=[_unit_vector(1)],
        )
        await _make_doc_with_chunks(
            pg_session,
            kb_type="faq",
            status="archived",
            embedding_model="itest-model",
            embeddings=[_unit_vector(1)],
        )

        results = await KbChunkRepository(pg_session).list_kb_chunk_by_similarity(
            "faq", _unit_vector(1), top_k=10, embedding_model="itest-model"
        )

        doc_repo = KbDocumentRepository(pg_session)
        for chunk, _ in results:
            doc = await doc_repo.get_kb_document_by_id(chunk.document_id)
            assert doc.status == "active"

    async def test_embedding_model_filter(self, pg_session):
        """不同模型的向量不可比：同向向量若模型不符必须被过滤。"""
        await _make_doc_with_chunks(
            pg_session,
            kb_type="sop",
            status="active",
            embedding_model="itest-model-a",
            embeddings=[_unit_vector(1)],
        )
        await _make_doc_with_chunks(
            pg_session,
            kb_type="sop",
            status="active",
            embedding_model="itest-model-b",
            embeddings=[_unit_vector(1)],
        )

        results = await KbChunkRepository(pg_session).list_kb_chunk_by_similarity(
            "sop", _unit_vector(1), top_k=10, embedding_model="itest-model-a"
        )

        assert results
        assert {c.embedding_model for c, _ in results} == {"itest-model-a"}
