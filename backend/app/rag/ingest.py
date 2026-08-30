"""入库管线：整篇知识原文 → 切块 → 批量嵌入 → 按 source_key 幂等落库。

写入模式与 docs/db-schema.md §2.6/§2.7 约定一致：
- 幂等：同 ``source_key`` 重入库，hash 相同直接跳过（省嵌入调用），
  hash 不同则更新文档并**整篇换块**（先删旧块再批量插新块）
- 事务边界收敛在 ``Database.session()``：预检读事务、换块写事务各自
  一个；embedding 网络调用放在事务之间，避免拿着数据库事务等网关
- kb_type 以首次入库为准（update 仅刷新 title/content_hash）；
  需变更类型请换 source_key 重新入库，status 归档语义由仓储层单独管理
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db.db import KbChunk, KbDocument
from app.db.engine import Database
from app.db.repositories import KbChunkRepository, KbDocumentRepository
from app.rag.chunking import chunk_text
from app.rag.embedding import KnowledgeEmbedder
from app.schemas.knowledge import (
    ALL_KB_TYPES,
    KB_SOURCE_TYPE_FILE,
    KB_SOURCE_TYPE_TEXT,
)

#: 入库动作：新建文档
INGEST_ACTION_CREATED = "created"
#: 入库动作：内容变更，整篇换块
INGEST_ACTION_UPDATED = "updated"
#: 入库动作：内容未变，跳过（未调用 embedding）
INGEST_ACTION_SKIPPED = "skipped"


@dataclass(slots=True, frozen=True)
class IngestResult:
    """单篇入库结果：动作、落库主键与最终块数（skipped 时为库中现有块数）。"""

    document_id: int
    source_key: str
    action: str
    chunk_count: int


class KnowledgeIngestor:
    """知识库入库器：持有 embedder 与 Database 门面，事务内构造仓储。"""

    def __init__(self, embedder: KnowledgeEmbedder, database: Database) -> None:
        self._embedder = embedder
        self._database = database

    async def ingest_text(
        self,
        *,
        kb_type: str,
        title: str,
        text: str,
        source_key: str,
        source_type: str = KB_SOURCE_TYPE_TEXT,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        """入库一篇手工维护/提取的文本知识。"""
        if kb_type not in ALL_KB_TYPES:
            msg = f"kb_type must be one of {sorted(ALL_KB_TYPES)}, got {kb_type!r}"
            raise ValueError(msg)
        if metadata is not None and not isinstance(metadata, dict):
            msg = f"metadata must be dict, got {type(metadata).__name__}"
            raise ValueError(msg)

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # 预检读事务：内容未变则跳过，embedding 调用整个省掉
        async with self._database.session() as session:
            existing = await KbDocumentRepository(session).get_kb_document_by_source_key(source_key)
            if existing is not None and existing.content_hash == content_hash:
                chunk_count = len(
                    await KbChunkRepository(session).list_kb_chunk_by_document_id(existing.id)
                )
                return IngestResult(
                    document_id=existing.id,
                    source_key=source_key,
                    action=INGEST_ACTION_SKIPPED,
                    chunk_count=chunk_count,
                )

        # 事务外完成切块与嵌入：网络调用不占数据库事务
        chunk_texts = chunk_text(text)
        if not chunk_texts:
            msg = f"text is empty after chunking, source_key={source_key!r}"
            raise ValueError(msg)
        vectors = await self._embedder.embed_documents(chunk_texts)

        # 写事务：查重 → 新建 / 整篇换块，单事务原子生效
        async with self._database.session() as session:
            document_repo = KbDocumentRepository(session)
            chunk_repo = KbChunkRepository(session)
            existing = await document_repo.get_kb_document_by_source_key(source_key)

            if existing is None:
                document = await document_repo.create_kb_document(
                    KbDocument(
                        kb_type=kb_type,
                        title=title,
                        source_type=source_type,
                        source_key=source_key,
                        content_hash=content_hash,
                    )
                )
                chunks = self._build_chunks(document, chunk_texts, vectors, metadata)
                await chunk_repo.bulk_create_kb_chunk(chunks)
                action, document_id = INGEST_ACTION_CREATED, document.id
            elif existing.content_hash == content_hash:
                # 预检后被并发重入库的兜底：内容已一致则不再换块
                chunk_count = len(await chunk_repo.list_kb_chunk_by_document_id(existing.id))
                return IngestResult(
                    document_id=existing.id,
                    source_key=source_key,
                    action=INGEST_ACTION_SKIPPED,
                    chunk_count=chunk_count,
                )
            else:
                await document_repo.update_kb_document_by_id(
                    existing.id, title=title, content_hash=content_hash
                )
                await chunk_repo.delete_kb_chunk_by_document_id(existing.id)
                chunks = self._build_chunks(existing, chunk_texts, vectors, metadata)
                await chunk_repo.bulk_create_kb_chunk(chunks)
                action, document_id = INGEST_ACTION_UPDATED, existing.id

        return IngestResult(
            document_id=document_id,
            source_key=source_key,
            action=action,
            chunk_count=len(chunk_texts),
        )

    async def ingest_file(
        self,
        path: str | Path,
        *,
        kb_type: str,
        title: str | None = None,
        source_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        """入库一个 utf-8 文本文件；source_key 默认 ``file:<path>``，与种子数据约定一致。"""
        file_path = Path(path)
        text = file_path.read_text(encoding="utf-8")
        return await self.ingest_text(
            kb_type=kb_type,
            title=title or file_path.stem,
            text=text,
            source_key=source_key or f"{KB_SOURCE_TYPE_FILE}:{file_path}",
            source_type=KB_SOURCE_TYPE_FILE,
            metadata=metadata,
        )

    def _build_chunks(
        self,
        document: KbDocument,
        chunk_texts: list[str],
        vectors: list[list[float]],
        metadata: dict[str, Any] | None,
    ) -> list[KbChunk]:
        """按块序构造 KbChunk 实体；向量数与块数错位时立即报错不入库。"""
        meta = metadata if metadata is not None else {}
        if len(chunk_texts) != len(vectors):
            msg = f"embedding count mismatch: {len(chunk_texts)} chunks but {len(vectors)} vectors"
            raise ValueError(msg)
        return [
            KbChunk(
                document_id=document.id,
                kb_type=document.kb_type,
                chunk_index=index,
                content=content,
                embedding=vector,
                embedding_model=self._embedder.model_name,
                meta=meta,
            )
            for index, (content, vector) in enumerate(zip(chunk_texts, vectors, strict=True))
        ]
