-- RAG 知识库建表脚本：与 docs/db-schema.md §2.6 / §2.7 严格一致
-- 用法：psql "$DATABASE_URL" -f scripts/kb_schema.sql
-- 前置：启用 pgvector 扩展需要 DB 执行权限；重复执行本脚本会因表已存在而报错

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE kb_documents (
    id           SERIAL PRIMARY KEY,
    kb_type      TEXT NOT NULL,                  -- faq / sop / compliance（白名单见 db-schema.md §2.8）
    title        TEXT NOT NULL,
    source_type  TEXT NOT NULL DEFAULT 'text',   -- mail / file / text（来源形态标注）
    source_key   TEXT NOT NULL UNIQUE,           -- 幂等键：同来源重入库不产生重复
    content_hash TEXT NOT NULL,                  -- 原文 SHA-256，变更检测：hash 不同才重嵌入
    status       TEXT NOT NULL DEFAULT 'active', -- active / archived，检索只取 active
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE kb_chunks (
    id              SERIAL PRIMARY KEY,
    document_id     INT NOT NULL,                -- 逻辑外键 → kb_documents.id（无物理 FK，与现有表一致）
    kb_type         TEXT NOT NULL,               -- 冗余自文档，类型过滤免 join
    chunk_index     INT NOT NULL,
    content         TEXT NOT NULL,               -- 检索命中后喂给 LLM 的列
    embedding       vector(1536) NOT NULL,
    embedding_model TEXT NOT NULL,               -- 向量归属模型，检索强制同模型匹配
    metadata        JSONB NOT NULL DEFAULT '{}', -- 块级补充过滤（标签/日期等）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX idx_kb_chunks_kb_type ON kb_chunks (kb_type);

COMMENT ON TABLE kb_documents IS '知识库文档表（RAG 输入）：文档级管理语义，检索粒度在 kb_chunks';
COMMENT ON COLUMN kb_documents.id IS '自增主键，kb_chunks.document_id 逻辑外键引用它';
COMMENT ON COLUMN kb_documents.kb_type IS '知识类型：faq（业务与产品 FAQ）/ sop（沟通 SOP 与语气）/ compliance（合规与红线规则），白名单见「知识类型表」';
COMMENT ON COLUMN kb_documents.title IS '文档标题，用于展示与运维定位';
COMMENT ON COLUMN kb_documents.source_type IS '来源形态：mail（邮件提取）/ file（文件导入）/ text（手工维护）';
COMMENT ON COLUMN kb_documents.source_key IS '来源幂等键（文件路径、URL、自造标识等），UNIQUE 保证同来源重入库不产生重复';
COMMENT ON COLUMN kb_documents.content_hash IS '原文 SHA-256，变更检测：hash 不同才重新分块与嵌入';
COMMENT ON COLUMN kb_documents.status IS '知识状态：active（生效，参与检索）/ archived（归档下线，仅留档审计）';
COMMENT ON COLUMN kb_documents.created_at IS '创建时间（UTC），插入后不可变';
COMMENT ON COLUMN kb_documents.updated_at IS '最后更新时间（UTC），重入库/归档时刷新';

COMMENT ON TABLE kb_chunks IS '知识库分块向量表（RAG 检索单元）：命中后 content 喂给 LLM';
COMMENT ON COLUMN kb_chunks.id IS '自增主键';
COMMENT ON COLUMN kb_chunks.document_id IS '所属文档 ID，逻辑外键 → kb_documents.id（无物理外键约束）；重入库时按它整篇换块';
COMMENT ON COLUMN kb_chunks.kb_type IS '冗余自文档的 kb_type，检索按类型过滤免 join，取值见「知识类型表」';
COMMENT ON COLUMN kb_chunks.chunk_index IS '文档内块序号（0 起）；UNIQUE(document_id, chunk_index)，命中后按序还原上下文';
COMMENT ON COLUMN kb_chunks.content IS '切片原文，检索命中后喂给 LLM 的就是这一列';
COMMENT ON COLUMN kb_chunks.embedding IS '1536 维向量，维度与 embedding 模型输出绑定（代码常量 KB_EMBEDDING_DIMENSIONS）';
COMMENT ON COLUMN kb_chunks.embedding_model IS '产出该向量的模型名；不同模型向量不可比，检索强制同模型匹配';
COMMENT ON COLUMN kb_chunks.metadata IS '块级补充过滤条件，JSONB 对象，示例：{"tags":["pricing"],"audience":"tob"}；热过滤字段（kb_type/status/embedding_model）为显式列，不在此列';
COMMENT ON COLUMN kb_chunks.created_at IS '入库时间（UTC），插入后不可变';
