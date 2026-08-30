# Agent 模块说明（AI 可读）

本目录承载基于 LangGraph 的**邮件意向分析 + 回复草稿**编排，只包含一个 langgraph：
分析图（`analysis_graph.py` 的 `build_email_analysis_graph`）。

流程：从 coordinator 拿到清洗后的文本 → 进入本图 → analyze 在原文上做结构化
意向分析 → 按主意图条件路由（垃圾/通知类直接结束，其余邮件检测语言并翻译为
中文）→ 第二次条件路由：主意图命中 `DRAFT_CATEGORY_BY_INTENT`（售前/售后
咨询类）且注入了 KnowledgeRetriever 时，进入草稿分支检索知识库起草回复草稿。
**草稿只落 `email_drafts` 待人工确认，本模块不发送邮件**；除草稿节点的
向量检索外不触碰 DB、不做文本清洗，DB 读写与文本清洗统一由
`email_coordinator.py`（`app/core/`）完成后注入。

## 唯一的 langgraph：邮件意向分析图（含条件草稿分支）

    START → analyze ──(按主意图条件路由)──→ detect_and_translate ──(草稿意图路由)──→ draft_presale / draft_aftersale → END
                    └─ primary_intent ∈ TRANSLATION_EXCLUDED_INTENTS → END          └─ 无映射 / 未注入 retriever → END

- `analyze`：调 `chat_model.with_structured_output(EmailAnalysisOutput)` 做结构化意向分析
- `detect_and_translate`：调 `chat_model.with_structured_output(EmailTranslationOutput)`
  单次 LLM 调用同时检测源语言并把主题/正文译为简体中文。短路顺序：
  主题正文均空 → `source_language="unknown"`；`_is_chinese_dominant_text`
  判定中文主导 → `source_language="zh"`（两者都不调 LLM）。
  译文只落库展示，不回灌 analyze。
- 条件边 `_route_translation_by_primary_intent`：`primary_intent` 属于
  `TRANSLATION_EXCLUDED_INTENTS`（单一来源在 `app/schemas/analysis.py`，
  当前仅 `spam_or_notice`）→ 直接 END，否则进 `detect_and_translate`。
- `draft_presale` / `draft_aftersale`（`_draft_node` 共享实现，售前查 `faq`、
  售后查 `sop` 知识库）：检索 → 质量门槛（无命中或最近余弦距离 >
  `DRAFT_MAX_COSINE_DISTANCE` 视为无相关知识，**不出草稿转人工**）→
  红线规则（coordinator 全量注入，非召回）+ 知识摘录（标注文档标题与距离）
  + 分层视图 + 客户语言拼 prompt → 单次 LLM 调用产出
  `EmailDraftOutput`（subject/body）。
- 条件边 `_route_after_translation`：意图不在
  `DRAFT_CATEGORY_BY_INTENT`（单一来源在 `app/schemas/draft.py`）或未注入
  retriever → END。
- analyze / detect_and_translate 各挂 `RetryPolicy(max_attempts=2,
  retry_on=(LLMInvocationError,))`，LLM 失败统一包装 `LLMInvocationError`
  原样穿透 `ainvoke`（节点自身零日志，调用链追踪靠 `trace_handle.py` 的
  `GraphTraceHandler`）。**草稿节点刻意不挂 RetryPolicy、内部全量降级**为
  `draft_skipped_reason`（检索失败记一条 warning）——草稿是附加产物，
  任何草稿失败不得影响已完成的意向分析。

### 状态（EmailAnalysisState）
- 输入：`email_id, account_id, subject, sender, sent_at, cleaned_text`；
  `compliance_rules`（coordinator 全量读出的 active 红线块文本，读失败降级为空）
- analyze 产出：`primary_intent, intents, reasoning_summary, entities, sentiment, priority, suggested_tools, llm_model`
- detect_and_translate 产出：`source_language, translated_subject, translated_text`
  （垃圾邮件与中文短路路径只有 `source_language`，译文键不出现）
- 草稿节点产出：`draft_category, draft_subject, draft_body, draft_sources,
  draft_model`（sources 每项含 `document_id / title / distance / snippet`）；
  降级路径只出 `draft_skipped_reason`
- 错误不经 state 传递：analyze/translate 节点抛 `LLMInvocationError`
  （`app/agent/errors.py`），由 coordinator `except AnalysisGraphError` 捕获后
  落库 `status="failed"`；草稿节点不抛错，降级原因走 state

### 构建方式
```python
graph = build_email_analysis_graph(chat_model, vision_model=None, knowledge_retriever=None)
result = await graph.ainvoke(initial_state)
```
依赖全部闭包注入（`chat_model` / `vision_model` / `knowledge_retriever`），
无模块级全局；`knowledge_retriever` 为 None 时草稿分支永不进入。

## EmailCoordinator 集中调度（由 Container 持有）

`Container` 持有 `EmailCoordinator`（`app/core/email_coordinator.py`），
`__init__` 内 eager 构建 `chat_model` 与 `analysis_graph`（含容器装配的
`knowledge_retriever`，embedding 未配置时容器降级传 None），缺 `LLM_API_KEY` 时
`build_chat_model` 即抛错不启动；CLI 零业务逻辑：

- `analyze_email(email_id)`：从 DB 读邮件 → 清洗正文 → 全量读红线规则 → 建初始状态 → 驱动分析图 → 落库分析结果 → 草稿落 `email_drafts`（幂等覆盖，status 重置 pending）→ 返回状态 dict（含 `draft`）
- `start_analyze(limit)`：批量分析未处理邮件（逐封独立事务）

调用路径为 `container.email_coordinator.*`；人工确认经 CLI
`draft_list` / `draft_review`（仅改状态，不发送邮件）。

## 职责边界
- `app/agent/`：仅 LLM 分析图（含草稿分支的向量检索），不依赖服务层
- `app/core/email_coordinator.py`：DB 读写 + 文本清洗 + 图驱动 + 草稿落库
- `app/services/preprocess.py`：纯函数清洗逻辑（由 coordinator 调用）
- `app/rag/`：检索门面 `KnowledgeRetriever`（由容器装配注入图）

## 源码索引
- 意向分析图：`app/agent/analysis_graph.py`（analyze / detect_and_translate / draft_presale / draft_aftersale / 条件边路由）
- 图层常量：`app/agent/constants.py`（语言检测区间 / LLM 调用超时与截断 / 重试次数 / 草稿检索门槛）
- Prompt 常量：`app/agent/prompts.py`（`EMAIL_ANALYSIS_SYSTEM_PROMPT` / `EMAIL_TRANSLATE_SYSTEM_PROMPT` / `DRAFT_PRESALE_SYSTEM_PROMPT` / `DRAFT_AFTERSALE_SYSTEM_PROMPT`）
- 分析 Schema：`app/schemas/analysis.py`（`EmailAnalysisOutput` / `EmailTranslationOutput` / `TRANSLATION_EXCLUDED_INTENTS` / `UNKNOWN_INTENT`）
- 草稿 Schema：`app/schemas/draft.py`（`EmailDraftOutput` / `DRAFT_CATEGORY_BY_INTENT` / 状态白名单）
- 文本清洗：`app/services/preprocess.py`（`preprocess_email_text` / `compose_email_view`）
- 编排器：`app/core/email_coordinator.py`
- ORM 模型与仓储：`app/db/db.py`（`EmailAnalysis`、`EmailDraft`）、`app/db/repositories.py`（`EmailAnalysisRepository` / `EmailDraftRepository`）
- 数据表：`email_analyses`（`docs/db-schema.md` §2.3）、`email_drafts`（§2.9）
