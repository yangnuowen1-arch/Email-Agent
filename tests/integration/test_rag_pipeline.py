"""RAG 管线集成测试（真 PostgreSQL + pgvector）：入库 → 检索全链路。

覆盖 sqlite 单测无法验证的行为：KnowledgeIngestor 的真实提交事务、
整篇换块在 PG 上的 id 语义、KnowledgeRetriever 经 ``<=>`` 的余弦排序、
embedding_model 过滤与 archived 文档排除的端到端效果。

运行前提：
    psql "$DATABASE_URL" -f scripts/kb_schema.sql      # 建表

用法：
    DATABASE_URL="postgresql+psycopg://..." pytest tests/integration -q

数据隔离：与 test_kb_repository.py 的「flush 后统一回滚」不同，本文件
走 KnowledgeIngestor 的真实提交路径（被测行为本身含 commit），因此采用
``itest:`` source_key 前缀 + fixture 前后显式清理，不在库里留残留数据。
"""

from __future__ import annotations

import os
import random

import pytest
from dotenv import load_dotenv
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.db import KbChunk, KbDocument
from app.db.engine import Database
from app.db.repositories import KbDocumentRepository
from app.rag.embedding import KnowledgeEmbedder
from app.rag.ingest import (
    INGEST_ACTION_CREATED,
    INGEST_ACTION_SKIPPED,
    INGEST_ACTION_UPDATED,
    KnowledgeIngestor,
)
from app.rag.retriever import KnowledgeRetriever
from app.schemas.knowledge import KB_EMBEDDING_DIMENSIONS

load_dotenv(override=False)

MODEL_NAME = "itest-fake-1536"
MARKER_A = "聚簇甲"
MARKER_B = "聚簇乙"


def _random_vector(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [round(rng.uniform(-1.0, 1.0), 6) for _ in range(KB_EMBEDDING_DIMENSIONS)]


class _ClusterEmbedderClient:
    """按文本关键词映射到两个确定性聚簇中心的 fake 客户端（无网络）。

    含 MARKER_A 的文本映射到中心 A，否则映射到中心 B；查询同样按关键词
    落到某一中心，从而构造「查询必然更接近某文档」的已知余弦序。
    """

    def __init__(self) -> None:
        self._centers = {MARKER_A: _random_vector(101), MARKER_B: _random_vector(202)}
        self.document_calls = 0
        self.query_calls = 0

    def _vector_for(self, text: str) -> list[float]:
        for marker, center in self._centers.items():
            if marker in text:
                return center
        return self._centers[MARKER_B]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        return [self._vector_for(t) for t in texts]

    async def aembed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._vector_for(text)


def _para(marker: str, ordinal: int) -> str:
    """构造一个 ~250 字的段落（超过 max_chars/2，保证两段落不装进同一块）。"""
    prefix = f"{marker}规则第{ordinal}条："
    return prefix + "条" * (250 - len(prefix))


def _doc_text(marker: str, paragraphs: int) -> str:
    return "\n\n".join(_para(marker, i) for i in range(1, paragraphs + 1))


@pytest.fixture
async def pg_database():
    """真 PG Database 门面；前后按 itest: 前缀清理，未配置环境时跳过。"""
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("DATABASE_URL 未设置，跳过 RAG 管线集成测试")

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM kb_documents LIMIT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"kb 表不存在（先执行 scripts/kb_schema.sql）：{type(exc).__name__}")

    db = Database(engine=engine, sessions=async_sessionmaker(engine, expire_on_commit=False))
    await _cleanup_itest_rows(db)  # 清理上次运行可能的残留
    yield db
    await _cleanup_itest_rows(db)
    await engine.dispose()


async def _cleanup_itest_rows(db: Database) -> None:
    """删除全部 itest: 前缀的知识数据（块先于文档，遵守逻辑外键顺序）。"""
    async with db.session() as session:
        await session.execute(
            delete(KbChunk).where(
                KbChunk.document_id.in_(
                    select(KbDocument.id).where(KbDocument.source_key.like("itest:%"))
                )
            )
        )
        await session.execute(delete(KbDocument).where(KbDocument.source_key.like("itest:%")))


def _make_ingestor(db: Database) -> tuple[KnowledgeIngestor, _ClusterEmbedderClient]:
    client = _ClusterEmbedderClient()
    embedder = KnowledgeEmbedder(model_name=MODEL_NAME, _client=client)
    return KnowledgeIngestor(embedder=embedder, database=db), client


def _make_retriever(
    db: Database, client: _ClusterEmbedderClient, *, model_name: str = MODEL_NAME
) -> KnowledgeRetriever:
    return KnowledgeRetriever(
        embedder=KnowledgeEmbedder(model_name=model_name, _client=client), database=db
    )


class TestIngestThenRetrieve:
    async def test_roundtrip_cosine_ordering(self, pg_database: Database) -> None:
        ingestor, client = _make_ingestor(pg_database)
        doc_a = await ingestor.ingest_text(
            kb_type="faq",
            title="聚簇甲知识",
            text=_doc_text(MARKER_A, 2),
            source_key="itest:pipeline-a",
            metadata={"tags": ["itest"]},
        )
        await ingestor.ingest_text(
            kb_type="faq",
            title="聚簇乙知识",
            text=_doc_text(MARKER_B, 2),
            source_key="itest:pipeline-b",
        )
        assert doc_a.action == INGEST_ACTION_CREATED
        assert doc_a.chunk_count == 2

        retriever = _make_retriever(pg_database, client)
        hits = await retriever.retrieve("faq", f"请解释{MARKER_A}相关规则")

        # 两篇 faq 文档的 4 个块同场竞争：查询落在中心 A，甲文档的块排最前
        assert len(hits) == 4
        assert hits[0].document_id == doc_a.document_id
        assert hits[0].distance == pytest.approx(0.0, abs=1e-5)
        assert hits[0].distance <= hits[-1].distance
        assert all(h.chunk.embedding_model == MODEL_NAME for h in hits)
        # doc A 入库时带了 metadata，doc B 未带：块级 meta 各自正确落库
        assert hits[0].chunk.meta == {"tags": ["itest"]}
        # 检索行带出所属文档标题（真实 join 数据）
        assert hits[0].document_title == "聚簇甲知识"

    async def test_reingest_same_content_skips(self, pg_database: Database) -> None:
        ingestor, client = _make_ingestor(pg_database)
        first = await ingestor.ingest_text(
            kb_type="sop",
            title="t",
            text=_doc_text(MARKER_B, 2),
            source_key="itest:pipeline-skip",
        )

        second = await ingestor.ingest_text(
            kb_type="sop",
            title="t",
            text=_doc_text(MARKER_B, 2),
            source_key="itest:pipeline-skip",
        )

        assert second.action == INGEST_ACTION_SKIPPED
        assert second.document_id == first.document_id
        assert second.chunk_count == first.chunk_count
        assert client.document_calls == 1  # skipped 路径未发起嵌入调用

    async def test_reingest_changed_content_replaces_chunks(self, pg_database: Database) -> None:
        ingestor, _ = _make_ingestor(pg_database)
        first = await ingestor.ingest_text(
            kb_type="compliance",
            title="旧版红线",
            text=_doc_text(MARKER_A, 2),
            source_key="itest:pipeline-update",
        )

        result = await ingestor.ingest_text(
            kb_type="compliance",
            title="新版红线",
            text=_doc_text(MARKER_A, 3),  # 2 块 → 3 块
            source_key="itest:pipeline-update",
        )

        assert result.action == INGEST_ACTION_UPDATED
        assert result.document_id == first.document_id
        assert result.chunk_count == 3

        async with pg_database.session() as session:
            doc = await KbDocumentRepository(session).get_kb_document_by_id(first.document_id)
            assert doc is not None
            assert doc.title == "新版红线"
            # PG 的 SERIAL 序列不复用 id：换块后现存块 id 必然是新分配的
            rows = await session.execute(
                select(KbChunk.id).where(KbChunk.document_id == first.document_id)
            )
            assert len({row[0] for row in rows.all()}) == 3

    async def test_retrieve_excludes_archived_document(self, pg_database: Database) -> None:
        ingestor, client = _make_ingestor(pg_database)
        doc_a = await ingestor.ingest_text(
            kb_type="faq",
            title="将被归档",
            text=_doc_text(MARKER_A, 2),
            source_key="itest:pipeline-archive",
        )
        await ingestor.ingest_text(
            kb_type="faq",
            title="仍然生效",
            text=_doc_text(MARKER_B, 2),
            source_key="itest:pipeline-alive",
        )

        async with pg_database.session() as session:
            await KbDocumentRepository(session).update_kb_document_by_id(
                doc_a.document_id, status="archived"
            )

        retriever = _make_retriever(pg_database, client)
        hits = await retriever.retrieve("faq", f"查询{MARKER_A}相关规则")

        assert hits, "归档后同类型仍应有其他文档可命中"
        assert doc_a.document_id not in {h.document_id for h in hits}

    async def test_retrieve_filters_by_embedding_model(self, pg_database: Database) -> None:
        ingestor, client = _make_ingestor(pg_database)
        await ingestor.ingest_text(
            kb_type="faq",
            title="t",
            text=_doc_text(MARKER_A, 2),
            source_key="itest:pipeline-model",
        )

        # 同向量不同模型名：向量不可比，检索必须不命中
        other_retriever = _make_retriever(pg_database, client, model_name="other-model")
        hits = await other_retriever.retrieve("faq", f"查询{MARKER_A}相关规则")

        assert hits == []
