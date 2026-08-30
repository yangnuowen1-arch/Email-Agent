"""rag.ingest 单元测试：幂等入库三态（created/updated/skipped）与整篇换块。

sqlite 内存库（kb 两表，embedding 经 with_variant 退化为 JSON 列）+
fake embedder，无网络；真实 pgvector 行为由 tests/integration 覆盖。
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.db import Base, KbChunk, KbDocument
from app.db.engine import Database
from app.db.repositories import KbChunkRepository, KbDocumentRepository
from app.rag.embedding import KnowledgeEmbedder
from app.rag.errors import EmbeddingDimensionError
from app.rag.ingest import (
    INGEST_ACTION_CREATED,
    INGEST_ACTION_SKIPPED,
    INGEST_ACTION_UPDATED,
    KnowledgeIngestor,
)
from app.schemas.knowledge import KB_EMBEDDING_DIMENSIONS


def _long_para(prefix: str, total: int) -> str:
    """构造指定总长度的段落（prefix + 填充字），用于控制切块后的块数。"""
    return prefix + "甲" * (total - len(prefix))


# 两个 305 字段落：305+2+305 > 500（默认 max_chars），各占一块 → 共 2 块
DOC_TEXT = _long_para("产品介绍：", 305) + "\n\n" + _long_para("购买方式：", 305)
# 三个 250 字段落：250+2+250 = 502 > 500，两两装不下 → 共 3 块
ALT_TEXT = (
    _long_para("称呼规范：", 250)
    + "\n\n"
    + _long_para("落款规范：", 250)
    + "\n\n"
    + _long_para("语气规范：", 250)
)


class FakeClient:
    """记录调用次数、按预设维度返回固定向量的 fake 客户端。"""

    def __init__(self, dims: int = KB_EMBEDDING_DIMENSIONS) -> None:
        self.dims = dims
        self.calls = 0

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.1] * self.dims for _ in texts]

    async def aembed_query(self, text: str) -> list[float]:
        self.calls += 1
        return [0.2] * self.dims


def _make_ingestor(database: Database, dims: int = KB_EMBEDDING_DIMENSIONS):
    client = FakeClient(dims=dims)
    embedder = KnowledgeEmbedder(model_name="fake-1536", _client=client)
    return KnowledgeIngestor(embedder=embedder, database=database), client


@pytest.fixture
async def database():
    """内存 SQLite（仅 kb 两表）包成 Database 门面，走真实 session() 事务语义。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[KbDocument.__table__, KbChunk.__table__])
        )
    db = Database(engine=engine, sessions=async_sessionmaker(engine, expire_on_commit=False))
    yield db
    await engine.dispose()


class TestIngestText:
    async def test_create_document_and_chunks(self, database: Database) -> None:
        ingestor, _ = _make_ingestor(database)

        result = await ingestor.ingest_text(
            kb_type="faq",
            title="产品 FAQ",
            text=DOC_TEXT,
            source_key="text:faq-v1",
            metadata={"tags": ["faq"], "audience": "all"},
        )

        assert result.action == INGEST_ACTION_CREATED
        assert result.document_id is not None
        assert result.chunk_count == 2  # 两个长段落各占一块

        async with database.session() as session:
            doc = await KbDocumentRepository(session).get_kb_document_by_source_key("text:faq-v1")
            assert doc is not None
            assert doc.kb_type == "faq"
            assert doc.source_type == "text"
            assert doc.status == "active"
            assert doc.content_hash == hashlib.sha256(DOC_TEXT.encode("utf-8")).hexdigest()

            chunks = await KbChunkRepository(session).list_kb_chunk_by_document_id(doc.id)
        assert [c.chunk_index for c in chunks] == [0, 1]
        assert all(c.embedding_model == "fake-1536" for c in chunks)
        assert all(c.meta == {"tags": ["faq"], "audience": "all"} for c in chunks)
        assert all(len(c.embedding) == KB_EMBEDDING_DIMENSIONS for c in chunks)

    async def test_reingest_same_content_skips_without_embedding(self, database: Database) -> None:
        ingestor, client = _make_ingestor(database)
        first = await ingestor.ingest_text(
            kb_type="faq", title="产品 FAQ", text=DOC_TEXT, source_key="text:faq-v1"
        )

        second = await ingestor.ingest_text(
            kb_type="faq", title="产品 FAQ", text=DOC_TEXT, source_key="text:faq-v1"
        )

        assert second.action == INGEST_ACTION_SKIPPED
        assert second.document_id == first.document_id
        assert second.chunk_count == first.chunk_count
        # skipped 路径不调 embedding（预检读事务直接命中 hash 相同）
        assert client.calls == 1

    async def test_reingest_changed_content_updates_and_rechunks(self, database: Database) -> None:
        ingestor, client = _make_ingestor(database)
        first = await ingestor.ingest_text(
            kb_type="faq", title="旧标题", text=DOC_TEXT, source_key="text:faq-v1"
        )
        old_contents = {c.content for c in await _chunks_of(database, first.document_id)}

        result = await ingestor.ingest_text(
            kb_type="faq",
            title="新标题",
            text=ALT_TEXT,  # 3 个段落 → 3 块，块数与旧版（2 块）不同
            source_key="text:faq-v1",
        )

        assert result.action == INGEST_ACTION_UPDATED
        assert result.document_id == first.document_id
        assert result.chunk_count == 3
        assert client.calls == 2  # 内容变更才会第二次嵌入

        async with database.session() as session:
            doc = await KbDocumentRepository(session).get_kb_document_by_source_key("text:faq-v1")
            assert doc is not None
            assert doc.title == "新标题"
            assert doc.content_hash == hashlib.sha256(ALT_TEXT.encode("utf-8")).hexdigest()
            document_id = doc.id

        chunks = await _chunks_of(database, document_id)
        # 整篇换块：sqlite 的 rowid 会复用，不能按 id 断言；用「旧内容全部
        # 下线 + 新序号从 0 起」验证先删后插
        assert [c.chunk_index for c in chunks] == [0, 1, 2]
        assert all(c.content not in old_contents for c in chunks)
        assert all(c.content in ALT_TEXT for c in chunks)

    async def test_kb_type_fixed_from_first_ingest(self, database: Database) -> None:
        """kb_type 以首次入库为准：同 source_key 换类型走更新路径但不改类型。"""
        ingestor, _ = _make_ingestor(database)
        await ingestor.ingest_text(kb_type="faq", title="t", text=DOC_TEXT, source_key="text:dup")

        result = await ingestor.ingest_text(
            kb_type="sop", title="t", text=ALT_TEXT, source_key="text:dup"
        )

        assert result.action == INGEST_ACTION_UPDATED
        async with database.session() as session:
            doc = await KbDocumentRepository(session).get_kb_document_by_source_key("text:dup")
            assert doc is not None
            assert doc.kb_type == "faq"

    async def test_empty_text_raises_without_side_effect(self, database: Database) -> None:
        ingestor, client = _make_ingestor(database)
        with pytest.raises(ValueError, match="empty after chunking"):
            await ingestor.ingest_text(
                kb_type="faq", title="t", text="   \n  ", source_key="text:blank"
            )
        assert client.calls == 0
        async with database.session() as session:
            assert (
                await KbDocumentRepository(session).get_kb_document_by_source_key("text:blank")
                is None
            )

    async def test_invalid_kb_type_raises_before_embedding(self, database: Database) -> None:
        ingestor, client = _make_ingestor(database)
        with pytest.raises(ValueError, match="kb_type"):
            await ingestor.ingest_text(
                kb_type="wiki", title="t", text=DOC_TEXT, source_key="text:bad"
            )
        assert client.calls == 0

    async def test_non_dict_metadata_raises(self, database: Database) -> None:
        ingestor, _ = _make_ingestor(database)
        with pytest.raises(ValueError, match="metadata must be dict"):
            await ingestor.ingest_text(
                kb_type="faq",
                title="t",
                text=DOC_TEXT,
                source_key="text:meta",
                metadata=["not-a-dict"],  # type: ignore[arg-type]
            )

    async def test_dimension_mismatch_aborts_ingest(self, database: Database) -> None:
        """网关模型返回维度不符时写库前拦截，不产生脏数据。"""
        ingestor, _ = _make_ingestor(database, dims=1024)
        with pytest.raises(EmbeddingDimensionError):
            await ingestor.ingest_text(
                kb_type="faq", title="t", text=DOC_TEXT, source_key="text:dim"
            )
        async with database.session() as session:
            assert (
                await KbDocumentRepository(session).get_kb_document_by_source_key("text:dim")
                is None
            )


class TestIngestFile:
    async def test_ingest_file_defaults(self, database: Database, tmp_path) -> None:
        file_path = tmp_path / "sop-tone-v1.md"
        file_path.write_text(DOC_TEXT, encoding="utf-8")
        ingestor, _ = _make_ingestor(database)

        result = await ingestor.ingest_file(file_path, kb_type="sop")

        assert result.action == INGEST_ACTION_CREATED
        async with database.session() as session:
            doc = await KbDocumentRepository(session).get_kb_document_by_source_key(
                f"file:{file_path}"
            )
            assert doc is not None
            assert doc.title == "sop-tone-v1"  # 默认标题取文件名 stem
            assert doc.source_type == "file"

    async def test_ingest_file_explicit_overrides(self, database: Database, tmp_path) -> None:
        file_path = tmp_path / "doc.md"
        file_path.write_text(DOC_TEXT, encoding="utf-8")
        ingestor, _ = _make_ingestor(database)

        await ingestor.ingest_file(
            file_path,
            kb_type="faq",
            title="自定义标题",
            source_key="file:custom-key",
        )

        async with database.session() as session:
            doc = await KbDocumentRepository(session).get_kb_document_by_source_key(
                "file:custom-key"
            )
            assert doc is not None
            assert doc.title == "自定义标题"


async def _chunks_of(database: Database, document_id: int) -> list[KbChunk]:
    """查询某文档现有块列表（按 chunk_index 排序），用于换块前后对比。"""
    async with database.session() as session:
        return await KbChunkRepository(session).list_kb_chunk_by_document_id(document_id)
