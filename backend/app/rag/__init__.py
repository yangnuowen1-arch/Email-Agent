"""RAG 知识库：切块 / 嵌入 / 入库 / 检索，为邮件回复草稿提供知识支撑。

当前仅提供 Python API 与 CLI 子命令（kb_ingest / kb_search），
与邮件业务链路零接线；M3 回复草稿图经闭包注入 KnowledgeRetriever。
数据落在 kb_documents / kb_chunks（docs/db-schema.md §2.6/§2.7）。
"""

from .chunking import chunk_text
from .embedding import EmbeddingClient, KnowledgeEmbedder, build_knowledge_embedder
from .errors import EmbeddingDimensionError, RAGError
from .ingest import (
    INGEST_ACTION_CREATED,
    INGEST_ACTION_SKIPPED,
    INGEST_ACTION_UPDATED,
    IngestResult,
    KnowledgeIngestor,
)
from .retriever import KnowledgeRetriever, RetrievedChunk

__all__ = [
    "chunk_text",
    "EmbeddingClient",
    "KnowledgeEmbedder",
    "build_knowledge_embedder",
    "EmbeddingDimensionError",
    "RAGError",
    "INGEST_ACTION_CREATED",
    "INGEST_ACTION_SKIPPED",
    "INGEST_ACTION_UPDATED",
    "IngestResult",
    "KnowledgeIngestor",
    "KnowledgeRetriever",
    "RetrievedChunk",
]
