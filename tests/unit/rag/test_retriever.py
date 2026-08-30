"""rag.retriever 单元测试：查询到仓储的参数透传与校验（stub 仓储，无 SQL）。

sqlite 执行不了 pgvector 的 ``<=>`` 操作符，真实余弦排序行为由
tests/integration/test_rag_pipeline.py 在真 PG 上覆盖；本文件用 stub
仓储验证 KnowledgeRetriever 的管道职责：
kb_type/top_k/embedding_model/查询向量按约定传给仓储。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.db import KbChunk
from app.db.engine import Database
from app.rag.embedding import KnowledgeEmbedder
from app.rag.errors import EmbeddingDimensionError
from app.rag.retriever import KnowledgeRetriever, RetrievedChunk
from app.schemas.knowledge import KB_EMBEDDING_DIMENSIONS


class FakeClient:
    """返回固定维度向量的 fake 客户端，记录查询调用。"""

    def __init__(self, dims: int = KB_EMBEDDING_DIMENSIONS) -> None:
        self.dims = dims

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dims for _ in texts]

    async def aembed_query(self, text: str) -> list[float]:
        return [0.2] * self.dims


def _make_embedder(dims: int = KB_EMBEDDING_DIMENSIONS) -> KnowledgeEmbedder:
    return KnowledgeEmbedder(model_name="fake-1536", _client=FakeClient(dims=dims))


def _make_chunk(document_id: int = 1, chunk_index: int = 0) -> KbChunk:
    return KbChunk(
        document_id=document_id,
        kb_type="faq",
        chunk_index=chunk_index,
        content="退款政策：7 天无理由。",
        embedding=[0.1] * KB_EMBEDDING_DIMENSIONS,
        embedding_model="fake-1536",
    )


def _stub_repo_factory(calls: list[dict], hits: list[tuple[KbChunk, float]]):
    """构造记录调用参数的 stub 仓储类，替身 KbChunkRepository。"""

    class _StubChunkRepository:
        def __init__(self, session) -> None:
            self.session = session

        async def list_kb_chunk_by_similarity(
            self,
            kb_type: str,
            query_embedding: list[float],
            *,
            top_k: int = 5,
            embedding_model: str | None = None,
        ) -> list[tuple[KbChunk, float]]:
            calls.append(
                {
                    "kb_type": kb_type,
                    "dims": len(query_embedding),
                    "top_k": top_k,
                    "embedding_model": embedding_model,
                }
            )
            return hits

    return _StubChunkRepository


@pytest.fixture
async def database():
    """内存 SQLite Database 门面：stub 仓储不触库，仅提供 session() 事务语义。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    yield Database(engine=engine, sessions=async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


class TestRetrieve:
    async def test_passes_type_topk_model_and_vector_to_repository(
        self, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []
        chunk = _make_chunk()
        monkeypatch.setattr(
            "app.rag.retriever.KbChunkRepository",
            _stub_repo_factory(calls, [(chunk, 0.25)]),
        )
        retriever = KnowledgeRetriever(_make_embedder(), database)

        hits = await retriever.retrieve("faq", "退款政策是什么")

        assert len(calls) == 1
        assert calls[0] == {
            "kb_type": "faq",
            "dims": KB_EMBEDDING_DIMENSIONS,
            "top_k": 5,
            "embedding_model": "fake-1536",
        }
        assert len(hits) == 1
        assert isinstance(hits[0], RetrievedChunk)
        assert hits[0].content == chunk.content
        assert hits[0].document_id == chunk.document_id
        assert hits[0].distance == pytest.approx(0.25)

    async def test_top_k_override_wins(
        self, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []
        monkeypatch.setattr("app.rag.retriever.KbChunkRepository", _stub_repo_factory(calls, []))
        retriever = KnowledgeRetriever(_make_embedder(), database, top_k=5)

        await retriever.retrieve("sop", "怎么写落款", top_k=2)

        assert calls[0]["top_k"] == 2

    async def test_bad_kb_type_raises(self, database: Database) -> None:
        retriever = KnowledgeRetriever(_make_embedder(), database)
        with pytest.raises(ValueError, match="kb_type"):
            await retriever.retrieve("wiki", "问题")

    @pytest.mark.parametrize("query", ["", "   "])
    async def test_empty_query_raises(self, database: Database, query: str) -> None:
        retriever = KnowledgeRetriever(_make_embedder(), database)
        with pytest.raises(ValueError, match="query must be non-empty"):
            await retriever.retrieve("faq", query)

    async def test_constructor_rejects_non_positive_top_k(self, database: Database) -> None:
        with pytest.raises(ValueError, match="top_k"):
            KnowledgeRetriever(_make_embedder(), database, top_k=0)

    async def test_dimension_mismatch_propagates(self, database: Database) -> None:
        retriever = KnowledgeRetriever(_make_embedder(dims=1024), database)
        with pytest.raises(EmbeddingDimensionError):
            await retriever.retrieve("faq", "问题")
