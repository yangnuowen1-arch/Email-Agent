"""rag.embedding 单元测试：维度校验与工厂错误映射（fake 客户端，无网络）。"""

from __future__ import annotations

import pytest

from app.core.settings import LLMConfig
from app.llm.errors import LLMConfigurationError
from app.rag.embedding import KnowledgeEmbedder, build_knowledge_embedder
from app.rag.errors import EmbeddingDimensionError
from app.schemas.knowledge import KB_EMBEDDING_DIMENSIONS


class FakeClient:
    """按预设维度返回固定向量的 fake 客户端，记录调用以断言短路行为。"""

    def __init__(self, dims: int = KB_EMBEDDING_DIMENSIONS) -> None:
        self.dims = dims
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [[0.1] * self.dims for _ in texts]

    async def aembed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return [0.2] * self.dims


def _make_embedder(dims: int = KB_EMBEDDING_DIMENSIONS) -> tuple[KnowledgeEmbedder, FakeClient]:
    client = FakeClient(dims=dims)
    embedder = KnowledgeEmbedder(model_name="fake-1536", _client=client)
    return embedder, client


class TestKnowledgeEmbedder:
    async def test_embed_documents_returns_vectors(self) -> None:
        embedder, _ = _make_embedder()
        vectors = await embedder.embed_documents(["a", "b"])
        assert len(vectors) == 2
        assert all(len(v) == KB_EMBEDDING_DIMENSIONS for v in vectors)

    async def test_embed_documents_empty_input_short_circuits(self) -> None:
        embedder, client = _make_embedder()
        assert await embedder.embed_documents([]) == []
        assert client.document_calls == []

    async def test_embed_query_returns_vector(self) -> None:
        embedder, _ = _make_embedder()
        vector = await embedder.embed_query("退款政策是什么")
        assert len(vector) == KB_EMBEDDING_DIMENSIONS

    async def test_wrong_dimension_documents_raises(self) -> None:
        embedder, _ = _make_embedder(dims=1024)
        with pytest.raises(EmbeddingDimensionError, match="returned 1024 dims"):
            await embedder.embed_documents(["a"])

    async def test_wrong_dimension_query_raises(self) -> None:
        embedder, _ = _make_embedder(dims=3072)
        with pytest.raises(EmbeddingDimensionError, match="returned 3072 dims"):
            await embedder.embed_query("问题")

    async def test_error_message_hints_config_fix(self) -> None:
        embedder, _ = _make_embedder(dims=1024)
        with pytest.raises(EmbeddingDimensionError, match="LLM_EMBEDDING_MODEL"):
            await embedder.embed_query("问题")


class TestBuildKnowledgeEmbedder:
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        cfg = LLMConfig(llm_api_key=None, llm_embedding_model="fake-embed")
        with pytest.raises(LLMConfigurationError, match="No API key"):
            build_knowledge_embedder(cfg)

    def test_missing_model_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_EMBEDDING_MODEL", raising=False)
        cfg = LLMConfig(llm_api_key="test-key-123", llm_embedding_model=None)
        with pytest.raises(LLMConfigurationError, match="No embedding model"):
            build_knowledge_embedder(cfg)

    def test_binds_model_name_from_config(self) -> None:
        cfg = LLMConfig(llm_api_key="test-key-123", llm_embedding_model="fake-embed")
        embedder = build_knowledge_embedder(cfg)
        assert isinstance(embedder, KnowledgeEmbedder)
        assert embedder.model_name == "fake-embed"
