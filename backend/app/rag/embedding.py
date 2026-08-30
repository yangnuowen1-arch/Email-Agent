"""知识库 embedding 门面：绑定「客户端 + 模型名 + 维度校验」为单一对象。

ingest 与 retriever 都依赖两件事：向量化调用本身，以及写入/比对用的
模型名（kb_chunks.embedding_model，检索强制同模型匹配）。把两者绑在
KnowledgeEmbedder 里，下游构造签名只收一个依赖，也杜绝了
「向量来自 A 模型、模型名却记成 B」的错配。

维度校验收口在此处：模型实际返回维度 ≠ KB_EMBEDDING_DIMENSIONS 时
抛 EmbeddingDimensionError，在写库与检索之前就拦截。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.settings import LLMConfig
from app.llm import build_embedding_model
from app.rag.errors import EmbeddingDimensionError
from app.schemas.knowledge import KB_EMBEDDING_DIMENSIONS


class EmbeddingClient(Protocol):
    """embedding 客户端最小协议：生产为 OpenAIEmbeddings，测试可注入 fake。"""

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（入库路径）。"""
        ...

    async def aembed_query(self, text: str) -> list[float]:
        """单条查询向量化（检索路径）。"""
        ...


@dataclass(slots=True)
class KnowledgeEmbedder:
    """embedding 客户端与其模型名的绑定，所有出口做维度校验。"""

    model_name: str
    _client: EmbeddingClient

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化；空输入直接返回空列表，不发起网络调用。"""
        if not texts:
            return []
        vectors = await self._client.aembed_documents(texts)
        self._check_dimensions(vectors, context=f"embed_documents({len(texts)} texts)")
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """单条查询向量化。"""
        vector = await self._client.aembed_query(text)
        self._check_dimensions([vector], context=f"embed_query({text[:30]!r}...)")
        return vector

    def _check_dimensions(self, vectors: list[list[float]], *, context: str) -> None:
        for vector in vectors:
            if len(vector) != KB_EMBEDDING_DIMENSIONS:
                msg = (
                    f"embedding model '{self.model_name}' returned {len(vector)} dims, "
                    f"but kb_chunks.embedding requires {KB_EMBEDDING_DIMENSIONS} "
                    f"(context: {context}); 检查 LLM_EMBEDDING_MODEL 是否选错，"
                    f"或为支持 dimensions 参数的模型设置 LLM_EMBEDDING_DIMENSIONS"
                )
                raise EmbeddingDimensionError(msg)


def build_knowledge_embedder(llm_config: LLMConfig) -> KnowledgeEmbedder:
    """按 LLMConfig 构建知识库 embedder（同网关 /embeddings 端点）。

    Raises LLMConfigurationError when no API key or no embedding model is configured.
    """
    client = build_embedding_model(llm_config)
    return KnowledgeEmbedder(
        model_name=llm_config.llm_embedding_model or "",
        _client=client,
    )
