"""检索器：自然语言查询 → 向量化 → 同模型余弦检索 active 知识块。

SQL 细节（kb_type/active/embedding_model 过滤 + ``<=>`` 排序 + 文档标题
join）收敛在 ``KbChunkRepository.list_kb_chunk_by_similarity``，本模块只负责
把「查询文本」翻译成「向量」再交给仓储；邮件分析图的草稿分支经闭包注入
本类，图的内部不碰数据库。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.db import KbChunk
from app.db.engine import Database
from app.db.repositories import KbChunkRepository
from app.rag.embedding import KnowledgeEmbedder
from app.schemas.knowledge import ALL_KB_TYPES


@dataclass(slots=True, frozen=True)
class RetrievedChunk:
    """单条检索命中：原始块 + 所属文档标题 + 余弦距离（越小越相似）。"""

    chunk: KbChunk
    document_title: str
    distance: float

    @property
    def content(self) -> str:
        """命中块的原文，草稿节点直接喂给 LLM。"""
        return self.chunk.content

    @property
    def document_id(self) -> int:
        """所属文档 ID，运维定位与知识出处核对用。"""
        return self.chunk.document_id


class KnowledgeRetriever:
    """知识库检索器：持有 embedder 与 Database 门面，事务内构造仓储。"""

    def __init__(self, embedder: KnowledgeEmbedder, database: Database, *, top_k: int = 5) -> None:
        if not isinstance(top_k, int) or top_k <= 0:
            msg = f"top_k must be positive int, got {top_k!r}"
            raise ValueError(msg)
        self._embedder = embedder
        self._database = database
        self._top_k = top_k

    async def retrieve(
        self, kb_type: str, query: str, *, top_k: int | None = None
    ) -> list[RetrievedChunk]:
        """按知识类型检索与 query 语义最近的 top_k 个分块，按距离升序。

        仅命中 active 文档的块，且强制与入库时同 embedding_model——
        不同模型的向量不可比。kb_type 不在白名单或 query 为空时抛 ValueError。
        """
        if kb_type not in ALL_KB_TYPES:
            msg = f"kb_type must be one of {sorted(ALL_KB_TYPES)}, got {kb_type!r}"
            raise ValueError(msg)
        if not isinstance(query, str) or not query.strip():
            msg = "query must be non-empty str"
            raise ValueError(msg)

        query_embedding = await self._embedder.embed_query(query)
        effective_top_k = self._top_k if top_k is None else top_k
        async with self._database.session() as session:
            hits = await KbChunkRepository(session).list_kb_chunk_by_similarity(
                kb_type,
                query_embedding,
                top_k=effective_top_k,
                embedding_model=self._embedder.model_name,
            )
        return [
            RetrievedChunk(
                chunk=row.chunk, document_title=row.document_title, distance=row.distance
            )
            for row in hits
        ]
