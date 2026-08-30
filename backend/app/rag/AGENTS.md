# RAG 模块说明（AI 可读）

本目录承载**知识库（RAG）管线**：整篇知识原文 → 切块 → embedding → 按
`source_key` 幂等入库（kb_documents / kb_chunks）→ 余弦检索。知识库为
邮件回复草稿提供三类知识支撑（faq / sop / compliance，含义见
`docs/db-schema.md` §2.8）。

**本模块与邮件业务链路零接线**：不 import 服务层/编排器，不读写邮件表。唯一的
消费方有两个：分析图草稿分支经闭包注入 `KnowledgeRetriever`（图内不碰 DB）；
`EmailCoordinator` 经 `list_kb_chunk_by_kb_type` 全量读取 compliance 红线块
（见下文「与草稿链的对接」）。对外入口只有 Python API 与 CLI 子命令
（`kb_ingest` / `kb_search`）。

## 入库链（kb_ingest）

    CLI kb_ingest → Container.knowledge_ingestor → KnowledgeIngestor.ingest_file
      → ingest_text 三段式：
        ① 预检读事务   按 source_key 查重，content_hash 相同 → SKIPPED（不调 embedding）
        ② 事务之外     chunk_text 切块 + embedder.embed_documents（网络调用）
        ③ 写事务       文档不存在 → 建文档 + 批量建块（CREATED）
                      hash 不同   → 更新文档 + 整篇换块（UPDATED：先删旧块再批量插新块）

- 幂等键 `source_key`：`ingest_file` 默认 `file:<路径>`；同键重入库三态
  CREATED / UPDATED / SKIPPED（常量与 `IngestResult` 在 `ingest.py`）。
- `content_hash` = 原文 SHA-256；块级 `metadata`（dict）整篇共用。
- `kb_type` 以首次入库为准（update 只刷新 title / content_hash），
  换类型必须换 `source_key`；status（active/archived）不由入库路径修改。
- 切块 `chunk_text` 是纯函数（空行分段 → 贪心装填 max_chars=500 →
  超长段落滑动窗口硬切 overlap=50），确定可复现；空白文本在发起
  embedding 调用之前就报错。

## 检索链（kb_search）

    CLI kb_search → Container.knowledge_retriever → KnowledgeRetriever.retrieve
      → embedder.embed_query（问题文本 → 1536 维向量）
      → KbChunkRepository.list_kb_chunk_by_similarity（<=> 余弦距离升序）

过滤三条件全部在仓储层拼装：`kb_type` 白名单精确匹配 + 仅 active 文档的
块 + 强制同 `embedding_model`（不同模型的向量不可比），并 join `kb_documents`
带出文档标题。SQL 细节不外泄，调用方（分析图的草稿分支）拿到的只是
`RetrievedChunk(chunk, document_title, distance)` 列表。

## 嵌入门面

`KnowledgeEmbedder`（`embedding.py`）把「embedding 客户端 + 模型名 +
维度校验」绑成一个对象：ingest 与 retriever 只依赖它，杜绝
「向量来自 A 模型、模型名记成 B」的错配。所有出口校验维度等于
`KB_EMBEDDING_DIMENSIONS`（1536，单一来源 `app/schemas/knowledge.py`），
不符抛 `EmbeddingDimensionError`（`errors.py`，类型化异常传播）。
生产客户端由 `build_embedding_model`（`app/llm/factory.py`）构建，
`check_embedding_ctx_length=False`——第三方 OpenAI 兼容网关不认本地
tiktoken 分词出的 token 数组，必须直接发原文。

## 事务与生命周期（AGENTS.md 硬规则在本模块的落点）

- 事务边界只在 `Database.session()`（`app/db/engine.py`）；仓储
  （`app/db/repositories.py`）只 flush 不提交
- embedding 网络调用永远在事务之外：预检 → 调网关 → 写事务
- `Container`（`app/core/container.py`）的 `knowledge_ingestor` /
  `knowledge_retriever` property 按需显式构造，不缓存、无全局单例；
  本模块无长驻资源，`close_all()` 不涉及

## 配置（LLMConfig，.env 驱动）

- `LLM_EMBEDDING_MODEL`：embedding 模型名（走同网关 /embeddings）；
  未配置时仅 rag 功能抛 `LLMConfigurationError`，邮件主链路不受影响
- `LLM_EMBEDDING_DIMENSIONS`：仅当网关模型支持 dimensions 参数时设置；
  未配置则由 embedder 按实际返回维度校验兜底

## 职责边界

- `app/rag/`：切块 / 嵌入绑定 / 入库编排 / 检索门面；用 kb 仓储但不碰邮件表
- `app/db/repositories.py`：kb 两表 SQL 与相似度语句（`_build_similarity_stmt`）
- `app/llm/factory.py`：OpenAI 兼容端点客户端工厂（chat + embeddings）
- `app/core/container.py`：组装与持有；`app/cli/main.py`：零业务逻辑入口
- 常量单一来源：`app/schemas/knowledge.py`（kb_type / source_type /
  status / 1536 维）；新增知识类型先改它，再同步 docs/db-schema.md §2.8

## 与草稿链的对接（已落地）

- FAQ / SOP：草稿节点（`draft_presale` / `draft_aftersale`）经闭包注入的
  `KnowledgeRetriever` 向量检索，命中带 `document_title` 供草稿 prompt 与
  `email_drafts.sources` 标注知识出处
- 合规红线：`EmailCoordinator._load_compliance_rules` 经
  `KbChunkRepository.list_kb_chunk_by_kb_type`（非向量全量，仅 active 文档）
  读出红线块文本，经 state `compliance_rules` 注入草稿 prompt——红线恒在场，
  不走向量召回（种子占位向量也不影响红线注入）

## 明确不做

- hnsw 向量索引：等真实 embedding 模型与数据量定型后单独加
- 草稿生成 / 状态流转（email_drafts）属于 agent 图与 coordinator 层职责，本模块不参与

## 源码索引

- 切块：`app/rag/chunking.py`（`chunk_text`）
- 嵌入门面：`app/rag/embedding.py`（`KnowledgeEmbedder` / `build_knowledge_embedder`）
- 入库编排：`app/rag/ingest.py`（`KnowledgeIngestor` / `IngestResult` / `INGEST_ACTION_*`）
- 检索门面：`app/rag/retriever.py`（`KnowledgeRetriever` / `RetrievedChunk`）
- 异常：`app/rag/errors.py`（`RAGError` / `EmbeddingDimensionError`）
- ORM 模型：`app/db/db.py`（`KbDocument` / `KbChunk`）；仓储：`app/db/repositories.py`
- 数据表：kb_documents / kb_chunks（`docs/db-schema.md` §2.6/§2.7/§2.8 与 2026-08-30 变更记录）
- 建表与种子 SQL：`scripts/kb_schema.sql` / `scripts/kb_seed.sql`
  （种子为 `seed-dummy-1536` 占位向量，真实模型检索天然不命中；回填 =
  用 kb_ingest 重灌同 source_key 内容，hash 变化自动走更新换块）
- 测试：`tests/unit/rag/`（sqlite + fake 客户端，三态入库剧本）、
  `tests/integration/test_rag_pipeline.py`（真 PG 端到端，itest: 前缀隔离）
