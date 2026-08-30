# db-schema.md — Email-Agent 数据库设计文档

> 变更约定：任何 schema 变更必须**先修改本文档**的表结构定义，再在「变更记录」节追加对应的 SQL 操作并备注时间。

## 1. ER 关系

```
email_accounts (1) ──────< (N) emails
     │                        │
 账号配置(输入)            已拉取邮件(输出)
     └── last_sync_uid ──────┘  ← 断点续传的桥梁
                                │
                ┌───────────────┼───────────────┬───────────────┐
                ▼               ▼                               ▼
        email_attachments  email_analyses                 email_drafts
        附件元数据+COS引用   结构化分析(输出)                回复草稿(输出)
        ← 按 email_id 关联  ← email_id 唯一                ← email_id 唯一

kb_documents (1) ──────< (N) kb_chunks
     │                        │
 知识文档(输入)            分块向量(检索单元)
     └── kb_type 冗余 ────────┘  ← 检索按类型过滤免 join
```

- 一条 `email_accounts` 记录对应一个真实邮箱，是程序的唯一输入源。
- `emails.account_id` 外键关联到账号，`(account_id, uid)` 唯一约束保证同一邮箱内邮件不重复入库。
- `email_analyses.email_id` 唯一约束保证一封邮件至多一份分析结果，支撑 `ON CONFLICT (email_id) DO UPDATE` 幂等重跑。
- `email_attachments.email_id` 逻辑外键关联到邮件，一行 = 一个附件的元数据 + COS 对象引用；**附件字节不上库**，存腾讯云 COS，DB 只存 `storage_url` / `storage_key`。
- `email_drafts.email_id` 唯一约束保证一封邮件至多一版草稿，重生成即整体覆盖（status 重置回 pending）；**草稿只落库待人工确认，本系统不发送邮件**。
- `kb_documents`/`kb_chunks` 是 RAG 知识库（回复草稿的知识支撑）：文档级管幂等重入库与归档，块级管向量检索；`kb_type` 从文档冗余到块，类型过滤不需要 join。草稿分支（售前查 faq、售后查 sop）经 `KnowledgeRetriever` 消费，`email_drafts.sources` 记录命中依据。

## 2. 表结构

### 2.1 email_accounts — 邮箱账号配置表（输入）

```sql
CREATE TABLE email_accounts (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    host          TEXT NOT NULL,
    port          INT  NOT NULL DEFAULT 993,
    protocol      TEXT NOT NULL DEFAULT 'imap',
    username      TEXT NOT NULL,
    password      TEXT NOT NULL,
    use_ssl       BOOL NOT NULL DEFAULT TRUE,
    folder        TEXT NOT NULL DEFAULT 'INBOX',
    enabled       BOOL NOT NULL DEFAULT TRUE,
    last_sync_uid BIGINT NOT NULL DEFAULT 0,
    last_sync_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE email_accounts IS '邮箱账号配置表（程序输入源）';
COMMENT ON COLUMN email_accounts.id IS '自增主键，emails 表外键引用它';
COMMENT ON COLUMN email_accounts.name IS '账号别名，用于日志与汇总报告展示';
COMMENT ON COLUMN email_accounts.host IS 'IMAP 服务器地址，如 imap.qq.com';
COMMENT ON COLUMN email_accounts.port IS '服务端口，SSL 下默认 993';
COMMENT ON COLUMN email_accounts.protocol IS '协议标识，factory 据此创建客户端；当前仅支持 imap';
COMMENT ON COLUMN email_accounts.username IS '登录用户名（通常为完整邮箱地址）';
COMMENT ON COLUMN email_accounts.password IS '登录密码/授权码；禁止写入任何日志';
COMMENT ON COLUMN email_accounts.use_ssl IS '是否启用 SSL/TLS 连接';
COMMENT ON COLUMN email_accounts.folder IS '要拉取的邮箱文件夹';
COMMENT ON COLUMN email_accounts.enabled IS '软开关：FALSE 的账号不会被调度';
COMMENT ON COLUMN email_accounts.last_sync_uid IS '增量断点：已成功入库的最大 UID；0 表示从未同步（首跑全量）';
COMMENT ON COLUMN email_accounts.last_sync_at IS '最近一次成功同步时间，仅运维观测用';
COMMENT ON COLUMN email_accounts.created_at IS '创建时间';
COMMENT ON COLUMN email_accounts.updated_at IS '最后更新时间';
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | SERIAL | PK | 自增主键，emails 表外键引用它 |
| name | TEXT | NOT NULL | 账号别名，用于日志与汇总报告展示 |
| host | TEXT | NOT NULL | IMAP 服务器地址，如 `imap.qq.com` |
| port | INT | NOT NULL, DEFAULT 993 | 服务端口，SSL 下默认 993 |
| protocol | TEXT | NOT NULL, DEFAULT 'imap' | 协议标识，factory 据此创建客户端；当前仅支持 `imap` |
| username | TEXT | NOT NULL | 登录用户名（通常为完整邮箱地址） |
| password | TEXT | NOT NULL | 登录密码/授权码；**禁止写入任何日志** |
| use_ssl | BOOL | NOT NULL, DEFAULT TRUE | 是否启用 SSL/TLS 连接 |
| folder | TEXT | NOT NULL, DEFAULT 'INBOX' | 要拉取的邮箱文件夹 |
| enabled | BOOL | NOT NULL, DEFAULT TRUE | 软开关：FALSE 的账号不会被调度 |
| last_sync_uid | BIGINT | NOT NULL, DEFAULT 0 | **增量断点**：已成功入库的最大 UID；0 表示从未同步（首跑全量） |
| last_sync_at | TIMESTAMPTZ | 可空 | 最近一次成功同步时间，仅运维观测用 |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 创建时间（UTC），插入后不可变 |
| updated_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 最后更新时间（UTC），随行更新刷新 |

例子: INSERT INTO email_accounts (name, host, username, password) VALUES ('xxx@qq.com','imap.qq.com','xxx@qq.com','xxxx授权码') RETURNING id;

### 2.2 emails — 已拉取邮件表（输出）

```sql
CREATE TABLE emails (
    id          SERIAL PRIMARY KEY,
    account_id  INT NOT NULL,
    uid         BIGINT NOT NULL,
    message_id  TEXT,
    subject     TEXT,
    sender      TEXT,
    recipients  TEXT[],
    sent_at     TIMESTAMPTZ,
    text_body   TEXT,
    html_body   TEXT,
    is_read     BOOL NOT NULL DEFAULT FALSE,
    fetched_at  TIMESTAMPTZ DEFAULT now(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, uid)
);

COMMENT ON TABLE emails IS '已拉取邮件表（程序输出）';
COMMENT ON COLUMN emails.id IS '自增主键';
COMMENT ON COLUMN emails.account_id IS '所属账号，逻辑外键 → email_accounts.id（无物理外键约束）';
COMMENT ON COLUMN emails.uid IS '邮箱服务器内该文件夹下的 UID，与 account_id 组成幂等键';
COMMENT ON COLUMN emails.message_id IS 'RFC822 Message-ID 头，便于跨系统追溯去重';
COMMENT ON COLUMN emails.subject IS '已解码的主题；解析失败存空串而非 NULL 报错';
COMMENT ON COLUMN emails.sender IS '发件人地址';
COMMENT ON COLUMN emails.recipients IS '收件人列表（to + cc 合并存储）';
COMMENT ON COLUMN emails.sent_at IS '邮件 Date 头解析结果';
COMMENT ON COLUMN emails.text_body IS '纯文本正文';
COMMENT ON COLUMN emails.html_body IS 'HTML 正文；与 text_body 至少一个非空';
COMMENT ON COLUMN emails.is_read IS '是否已处理（agent 读取后置 TRUE）；FALSE 表示未读';
COMMENT ON COLUMN emails.fetched_at IS '本地拉取时间';
COMMENT ON COLUMN emails.created_at IS '创建时间（UTC），插入后不可变';
COMMENT ON COLUMN emails.updated_at IS '最后更新时间（UTC），随行更新刷新';
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | SERIAL | PK | 自增主键 |
| account_id | INT | NOT NULL，逻辑外键 → email_accounts.id（无物理外键约束） | 所属账号 |
| uid | BIGINT | NOT NULL | 邮箱服务器内该文件夹下的 UID，与 account_id 组成幂等键 |
| message_id | TEXT | 可空 | RFC822 Message-ID 头，便于跨系统追溯去重 |
| subject | TEXT | 可空 | 已解码的主题；解析失败存空串而非 NULL 报错 |
| sender | TEXT | 可空 | 发件人地址 |
| recipients | TEXT[] | 可空 | 收件人列表（to + cc 合并存储） |
| sent_at | TIMESTAMPTZ | 可空 | 邮件 Date 头解析结果 |
| text_body | TEXT | 可空 | 纯文本正文 |
| html_body | TEXT | 可空 | HTML 正文；与 text_body 至少一个非空 |
| is_read | BOOL | NOT NULL, DEFAULT FALSE | 是否已处理（agent 读取后置 TRUE）；FALSE 表示未读 |
| fetched_at | TIMESTAMPTZ | DEFAULT now() | 本地拉取时间 |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 创建时间（UTC），插入后不可变 |
| updated_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 最后更新时间（UTC），随行更新刷新 |

写入方式：批量 `INSERT ... ON CONFLICT (account_id, uid) DO NOTHING`。

### 2.3 email_analyses — 邮件结构化分析表（输出）

```sql
CREATE TABLE email_analyses (
    id                SERIAL PRIMARY KEY,
    email_id          INT  NOT NULL UNIQUE,
    account_id        INT  NOT NULL,
    primary_intent    TEXT NOT NULL,
    intents           JSONB NOT NULL DEFAULT '[]',
    reasoning_summary TEXT NOT NULL DEFAULT '',
    entities          JSONB NOT NULL DEFAULT '{}',
    sentiment         TEXT NOT NULL DEFAULT 'neutral',
    priority          TEXT NOT NULL DEFAULT 'P2',
    suggested_tools   JSONB NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'analyzed',
    error             TEXT,
    model             TEXT,
    source_language   TEXT,
    translated_subject TEXT,
    translated_text   TEXT,
    intent_evidence_source TEXT NOT NULL DEFAULT 'body',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE email_analyses IS '邮件结构化分析表（agent 节点 1 输出）';
COMMENT ON COLUMN email_analyses.id IS '自增主键';
COMMENT ON COLUMN email_analyses.email_id IS '分析对象邮件 ID，逻辑外键 → emails.id，唯一约束保证一封邮件至多一份分析';
COMMENT ON COLUMN email_analyses.account_id IS '冗余存储所属账号，便于按账号查询分析结果';
COMMENT ON COLUMN email_analyses.primary_intent IS '核心主意图，取值见「意图分类表」（如 cancel_order 取消订单/退订服务、refund_request 退款申请、meeting_request 会议/日程请求），未知时为 unknown_manual_review（无法判定，待人工复核）';
COMMENT ON COLUMN email_analyses.intents IS '多意图列表，JSONB 数组，每项含 category（意图标识，取值见「意图分类表」，如 cancel_order 取消订单/退订服务）/confidence（置信度 0-1）/reasoning（推导依据），示例：[{"category":"cancel_order","confidence":0.95,"reasoning":"用户要求取消"}]';
COMMENT ON COLUMN email_analyses.reasoning_summary IS 'AI 对整封邮件处理逻辑的全局综合判定总结';
COMMENT ON COLUMN email_analyses.entities IS '提取的关键业务实体，JSONB 对象，示例：{"order_id":"ORD-123","date":"2026-08-28","amount":"128.00"}';
COMMENT ON COLUMN email_analyses.sentiment IS '发件人情绪：positive / neutral / negative / angry / urgent';
COMMENT ON COLUMN email_analyses.priority IS '处理优先级：P0（极紧急/故障）P1（高）P2（中）P3（低）';
COMMENT ON COLUMN email_analyses.suggested_tools IS '建议后续调用的 Tool 函数名列表，JSONB 数组，示例：["summarize_emails"]';
COMMENT ON COLUMN email_analyses.status IS '分析状态：analyzed（成功）/ failed（LLM 异常或解析失败）';
COMMENT ON COLUMN email_analyses.error IS '失败原因，仅 status=failed 时非空';
COMMENT ON COLUMN email_analyses.model IS '产出该分析的模型名（如 gpt-4o-mini），便于回溯';
COMMENT ON COLUMN email_analyses.source_language IS '检测到的源语言 ISO 639-1 代码（detect_and_translate 节点产出，如 en/ja/zh/unknown），中文启发式短路或垃圾邮件跳过翻译时为空';
COMMENT ON COLUMN email_analyses.translated_subject IS '主题中文译文，仅非中文业务邮件非空（detect_and_translate 节点产出）';
COMMENT ON COLUMN email_analyses.translated_text IS '正文中文译文，仅非中文业务邮件非空（detect_and_translate 节点产出）';
COMMENT ON COLUMN email_analyses.intent_evidence_source IS '主意图的证据来源：body（壳层正文）/ attached_email（.eml 转发邮件附件）/ image（图片视觉识别）/ mixed（多层综合）';
COMMENT ON COLUMN email_analyses.created_at IS '首次分析时间';
COMMENT ON COLUMN email_analyses.updated_at IS '最后更新时间，ON CONFLICT DO UPDATE 时刷新';

-- 唯一索引由 UNIQUE 约束自动创建，写入方式：
-- INSERT INTO email_analyses (...) VALUES (...)
-- ON CONFLICT (email_id) DO UPDATE SET ...;
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | SERIAL | PK | 自增主键 |
| email_id | INT | NOT NULL, UNIQUE | 分析对象邮件 ID，逻辑外键 → emails.id |
| account_id | INT | NOT NULL | 冗余所属账号，便于按账号查询 |
| primary_intent | TEXT | NOT NULL | 核心主意图（见意图分类表），用于路由；未知为 unknown_manual_review（无法判定，待人工复核） |
| intents | JSONB | NOT NULL, DEFAULT '[]' | 多意图列表，每项含 category（见意图分类表）/confidence/reasoning |
| reasoning_summary | TEXT | NOT NULL, DEFAULT '' | AI 全局综合判定总结 |
| entities | JSONB | NOT NULL, DEFAULT '{}' | 提取的关键业务实体（order_id/date/amount 等） |
| sentiment | TEXT | NOT NULL, DEFAULT 'neutral' | 发件人情绪：positive/neutral/negative/angry/urgent |
| priority | TEXT | NOT NULL, DEFAULT 'P2' | 处理优先级：P0/P1/P2/P3 |
| suggested_tools | JSONB | NOT NULL, DEFAULT '[]' | 建议调用的 Tool 名列表 |
| status | TEXT | NOT NULL, DEFAULT 'analyzed' | analyzed（成功）/ failed（异常） |
| error | TEXT | 可空 | 失败原因，仅 status=failed 时非空 |
| model | TEXT | 可空 | 产出分析的模型名，便于回溯 |
| source_language | TEXT | 可空 | 检测到的源语言 ISO 639-1 代码（en/ja/zh/unknown），未翻译为空 |
| translated_subject | TEXT | 可空 | 主题中文译文，仅非中文业务邮件非空 |
| translated_text | TEXT | 可空 | 正文中文译文，仅非中文业务邮件非空 |
| intent_evidence_source | TEXT | NOT NULL, DEFAULT 'body' | 主意图的证据来源：body（壳层正文）/ attached_email（.eml 转发邮件附件）/ image（图片视觉识别）/ mixed（多层综合） |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 首次分析时间 |
| updated_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 最后更新时间，ON CONFLICT DO UPDATE 时刷新 |

写入方式：`INSERT ... ON CONFLICT (email_id) DO UPDATE`，支持重跑幂等。

### 2.4 email_attachments — 邮件附件表（输出）

```sql
CREATE TABLE email_attachments (
    id             SERIAL PRIMARY KEY,
    email_id       INT  NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'document',
    filename       TEXT NOT NULL DEFAULT '',
    content_type   TEXT NOT NULL DEFAULT '',
    disposition    TEXT,
    content_id     TEXT,
    size           INT  NOT NULL DEFAULT 0,
    storage_url    TEXT,
    storage_key    TEXT,
    extracted_text TEXT,
    extracted_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_email_attachments_email_id ON email_attachments(email_id);

COMMENT ON TABLE email_attachments IS '邮件附件表：元数据 + 腾讯云 COS 对象引用，附件字节不上库';
COMMENT ON COLUMN email_attachments.id IS '自增主键';
COMMENT ON COLUMN email_attachments.email_id IS '所属邮件 ID，逻辑外键 → emails.id（无物理外键约束）';
COMMENT ON COLUMN email_attachments.kind IS '附件类别：image（图片，含内嵌 cid 图）/ email（.eml / message/rfc822）/ document（其他）';
COMMENT ON COLUMN email_attachments.filename IS '附件文件名，可能缺失';
COMMENT ON COLUMN email_attachments.content_type IS 'MIME 内容类型，如 image/png、message/rfc822';
COMMENT ON COLUMN email_attachments.disposition IS 'Content-Disposition：inline / attachment，可能缺失';
COMMENT ON COLUMN email_attachments.content_id IS '内嵌资源 Content-ID（去尖括号），仅 inline 图片等场景非空';
COMMENT ON COLUMN email_attachments.size IS '附件大小（字节），以解析时实际读取为准';
COMMENT ON COLUMN email_attachments.storage_url IS 'COS 对象访问 URL；仅存元数据（未上传）时为 NULL';
COMMENT ON COLUMN email_attachments.storage_key IS 'COS 对象键，SDK 拉取与清理按 key 操作';
COMMENT ON COLUMN email_attachments.extracted_text IS '内容提取缓存：.eml 解析文本 / 图片识别文本；未提取为 NULL';
COMMENT ON COLUMN email_attachments.extracted_at IS '最近一次提取时间';
COMMENT ON COLUMN email_attachments.created_at IS '入库时间（UTC），插入后不可变';
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | SERIAL | PK | 自增主键 |
| email_id | INT | NOT NULL，逻辑外键 → emails.id | 所属邮件 |
| kind | TEXT | NOT NULL, DEFAULT 'document' | 附件类别：image / email / document |
| filename | TEXT | NOT NULL, DEFAULT '' | 附件文件名，可能缺失 |
| content_type | TEXT | NOT NULL, DEFAULT '' | MIME 内容类型 |
| disposition | TEXT | 可空 | Content-Disposition：inline / attachment |
| content_id | TEXT | 可空 | 内嵌资源 Content-ID（去尖括号） |
| size | INT | NOT NULL, DEFAULT 0 | 附件大小（字节） |
| storage_url | TEXT | 可空 | COS 对象访问 URL；未上传为 NULL |
| storage_key | TEXT | 可空 | COS 对象键，SDK 拉取/清理用 |
| extracted_text | TEXT | 可空 | 内容提取缓存（.eml 解析文本 / 图片识别文本），未提取为 NULL |
| extracted_at | TIMESTAMPTZ | 可空 | 最近一次提取时间 |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 入库时间（UTC） |

写入方式：抓取时 `INSERT`（附件先上传 COS 再写元数据，同账号事务内与邮件插入原子提交）；
分析时成功提取后 `UPDATE extracted_text / extracted_at`（缓存，重复分析不重复拉取与调用）。

### 2.5 意图分类表（intent categories）

> 英文标识是代码中的枚举值（`app/schemas/analysis.py` 的 `ALL_INTENTS`，单一来源），
> LLM 输出必须从本表选择；白名单外值校验失败 → 分析走 fallback（status=failed）。
> 新增意图先扩展代码常量，再同步本表。

| 英文标识 | 中文含义 | 场景分组 |
|---|---|---|
| cancel_order | 取消订单/退订服务 | ToC（消费者） |
| refund_request | 退款申请 | ToC（消费者） |
| order_status_query | 订单状态查询/物流追踪 | ToC（消费者） |
| invoice_query | 发票/账单查询 | ToC（消费者） |
| meeting_request | 会议/日程请求 | ToC（消费者） |
| complaint | 投诉/不满表达 | ToC（消费者） |
| pre_sales_consult | 售前咨询：产品信息/材质/价格/优惠券活动/库存/发货时效等购买前问题 | ToC（消费者） |
| after_sales_consult | 售后咨询：退换货政策/保修维护/使用方法等售后问题 | ToC（消费者） |
| spam_or_notice | 垃圾邮件/系统通知/广告 | ToC（消费者） |
| other | 无法归类的消费者意图 | ToC（消费者） |
| contract | 合同/协议相关 | ToB（企业） |
| payment | 付款/结算相关 | ToB（企业） |
| partnership | 合作/商务洽谈 | ToB（企业） |
| technical_issue | 技术问题/故障报告 | ToB（企业） |
| account_management | 账号/权限管理 | ToB（企业） |
| unknown_manual_review | 无法判定，待人工复核 | 兜底 |

`primary_intent` 与 `intents[].category` 均取本表枚举值。

### 2.6 kb_documents — 知识库文档表（RAG 输入）

```sql
-- 前置：启用 pgvector 扩展（kb_chunks.embedding 依赖，需 DB 执行权限）
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE kb_documents (
    id           SERIAL PRIMARY KEY,
    kb_type      TEXT NOT NULL,
    title        TEXT NOT NULL,
    source_type  TEXT NOT NULL DEFAULT 'text',
    source_key   TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | SERIAL | PK | 自增主键，kb_chunks 逻辑外键引用它 |
| kb_type | TEXT | NOT NULL | 知识类型（见知识类型表 §2.8）：faq / sop / compliance |
| title | TEXT | NOT NULL | 文档标题，展示与运维定位用 |
| source_type | TEXT | NOT NULL, DEFAULT 'text' | 来源形态：mail / file / text |
| source_key | TEXT | NOT NULL, UNIQUE | **来源幂等键**：同来源重入库不产生重复数据 |
| content_hash | TEXT | NOT NULL | 原文 SHA-256，变更检测：hash 不同才重嵌入 |
| status | TEXT | NOT NULL, DEFAULT 'active' | active（参与检索）/ archived（归档下线，仅留档） |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 创建时间（UTC），插入后不可变 |
| updated_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 最后更新时间（UTC），随行更新刷新 |

写入方式：先按 `source_key` 查重——不存在则插入，存在且 `content_hash` 不同则更新文档并整篇换块（见 §2.7），hash 相同则跳过（省嵌入调用）。实现：`app/rag/ingest.py` 的 `KnowledgeIngestor`（预检读事务 → 事务外嵌入 → 写事务新建/换块）。

### 2.7 kb_chunks — 知识库分块向量表（RAG 检索单元）

```sql
CREATE TABLE kb_chunks (
    id              SERIAL PRIMARY KEY,
    document_id     INT NOT NULL,
    kb_type         TEXT NOT NULL,
    chunk_index     INT NOT NULL,
    content         TEXT NOT NULL,
    embedding       vector(1536) NOT NULL,
    embedding_model TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX idx_kb_chunks_kb_type ON kb_chunks (kb_type);

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
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | SERIAL | PK | 自增主键 |
| document_id | INT | NOT NULL，逻辑外键 → kb_documents.id | 所属文档 |
| kb_type | TEXT | NOT NULL | 冗余自文档，类型过滤免 join（见 §2.8） |
| chunk_index | INT | NOT NULL, UNIQUE(document_id, chunk_index) | 文档内块序号（0 起），命中后按序还原上下文 |
| content | TEXT | NOT NULL | 切片原文，检索命中后喂给 LLM |
| embedding | vector(1536) | NOT NULL | 块向量，维度与 embedding 模型绑定 |
| embedding_model | TEXT | NOT NULL | 产出向量的模型名，检索强制同模型匹配 |
| metadata | JSONB | NOT NULL, DEFAULT '{}' | 块级补充过滤条件（tags/audience 等） |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 入库时间（UTC），插入后不可变 |

写入方式：重入库时**整篇换块**——先 `DELETE WHERE document_id = :id` 再批量插入新块，同一事务内完成。
检索方式：`SELECT ... WHERE kb_type = :t AND embedding_model = :m ORDER BY embedding <=> :query LIMIT :k`（余弦距离，仅 active 文档的块参与）。实现：`app/rag/retriever.py` 的 `KnowledgeRetriever`（查询文本先经 embedding 模型向量化，再走 `KbChunkRepository.list_kb_chunk_by_similarity`，检索行 join `kb_documents` 带出文档标题，供草稿上下文标注知识出处）。
向量索引（ivfflat/hnsw）待 M2 embedding 模型与维度定型后再加——M2 已落地 rag 管线（`app/rag/`）与 `LLM_EMBEDDING_MODEL` 配置，真实模型定型后仍单独加索引。

### 2.8 知识类型表（kb types）

> 英文标识是代码中的枚举值（`app/schemas/knowledge.py` 的 `ALL_KB_TYPES`，单一来源），
> 写入 `kb_documents.kb_type` / `kb_chunks.kb_type` 前按白名单校验。
> 新增知识类型先扩展代码常量，再同步本表。

| 英文标识 | 中文含义 | 业务用途 |
|---|---|---|
| faq | 业务与产品 FAQ | 产品/服务介绍、报价、交付周期、技术问答 → 提供准确业务事实，防止模型杜撰 |
| sop | 沟通 SOP 与语气 | 商务礼仪（称呼/落款）、场景模板（报价回复/改约/婉拒推销）→ 保持对外沟通专业一致 |
| compliance | 合规与红线规则 | 不可承诺事项（未批准折扣/未审阅法务条款）、转人工触发词 → 划定安全边界，草稿不越线 |

`kb_documents.kb_type` 与 `kb_chunks.kb_type` 均取本表枚举值；来源形态 `source_type` 白名单（mail / file / text）同样定义在 `app/schemas/knowledge.py`。

### 2.9 email_drafts — 回复草稿表（输出，人工确认流）

```sql
CREATE TABLE email_drafts (
    id         SERIAL PRIMARY KEY,
    email_id   INT  NOT NULL UNIQUE,
    account_id INT  NOT NULL,
    category   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    sources    JSONB NOT NULL DEFAULT '[]',
    model      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_email_drafts_account_id ON email_drafts(account_id);

COMMENT ON TABLE email_drafts IS '回复草稿表（输出）：待人工确认，本系统不发送邮件';
COMMENT ON COLUMN email_drafts.id IS '自增主键';
COMMENT ON COLUMN email_drafts.email_id IS '草稿归属邮件 ID，逻辑外键 → emails.id（无物理外键约束）；UNIQUE 保证一封邮件一版草稿，重生成即整体覆盖';
COMMENT ON COLUMN email_drafts.account_id IS '冗余所属账号，便于按账号查询草稿';
COMMENT ON COLUMN email_drafts.category IS '草稿类别：presale（售前咨询，检索 faq 知识）/ aftersale（售后问题，检索 sop 知识）';
COMMENT ON COLUMN email_drafts.status IS '确认状态：pending（待人工确认）/ approved（人工确认可用）/ rejected（人工否决）；重生成后重置回 pending';
COMMENT ON COLUMN email_drafts.subject IS '回复主题（草稿节点产出，原主题前加 Re: 前缀）';
COMMENT ON COLUMN email_drafts.body IS '回复正文（草稿节点产出，使用客户来信语言，仅基于知识库摘录与礼貌话术）';
COMMENT ON COLUMN email_drafts.sources IS '检索依据，JSONB 数组：[{"document_id":1,"title":"...","distance":0.32,"snippet":"..."}]，人工核对草稿事实用';
COMMENT ON COLUMN email_drafts.model IS '生成草稿的模型名';
COMMENT ON COLUMN email_drafts.created_at IS '首次生成时间（UTC）';
COMMENT ON COLUMN email_drafts.updated_at IS '最后更新时间（UTC），重生成/状态变更时刷新';
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | SERIAL | PK | 自增主键 |
| email_id | INT | NOT NULL, UNIQUE | 草稿归属邮件，唯一约束支撑 `ON CONFLICT (email_id) DO UPDATE` 幂等重生成 |
| account_id | INT | NOT NULL, INDEX | 冗余所属账号 |
| category | TEXT | NOT NULL | presale（售前）/ aftersale（售后），白名单 `app/schemas/draft.py` |
| status | TEXT | NOT NULL, DEFAULT 'pending' | pending / approved / rejected（人工确认流） |
| subject | TEXT | NOT NULL | 回复主题 |
| body | TEXT | NOT NULL | 回复正文 |
| sources | JSONB | NOT NULL, DEFAULT '[]' | 检索依据 `[{document_id, title, distance, snippet}]` |
| model | TEXT | | 生成草稿的模型名 |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 首次生成时间（UTC） |
| updated_at | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 最后更新时间（UTC） |

写入方式：分析图草稿分支（`draft_presale` / `draft_aftersale` 节点）产出 subject/body 与检索依据，由 `EmailCoordinator._save_draft` 经 `EmailDraftRepository.upsert_email_draft` 落库——`ON CONFLICT (email_id) DO UPDATE` 整体覆盖且 `status` 重置回 pending。
质量门槛：知识库检索无命中或最近余弦距离超过 `DRAFT_MAX_COSINE_DISTANCE`（代码常量，与 embedding 模型相关）时**不出草稿**（`draft_skipped_reason="no_relevant_knowledge"`），邮件留在分析结果层转人工——低置信不硬答。
确认流：`draft_list` / `draft_review` CLI 仅读写本表状态，**发送邮件在系统之外由人工完成**。

## 3. 设计说明

### 为什么幂等键是 `(account_id, uid)` 而不是 message_id？

- UID 是 IMAP 协议内**同一邮箱文件夹中稳定递增**的标识，天然适合做增量断点（`last_sync_uid`）；
- message_id 由发件方生成，可能缺失或格式不合规，不适合做主键约束；
- 二者职责不同：uid 管「同步位置」，message_id 管「跨系统追溯」。

### 断点语义

1. 同步前读取 `last_sync_uid = N`；
2. 仅拉取 `UID > N` 的邮件；
3. 本批全部成功入库后，将 `last_sync_uid` 更新为本批最大 UID、刷新 `last_sync_at`；
4. 中途失败则不推进断点，下次重拉（配合 ON CONFLICT 幂等，重拉无副作用）。

### 为什么分析状态独立成表而不是加 `emails.status`？

- `emails` 属于同步层产物，保持只追加语义（sync 写入后不修改），便于独立排查同步问题。
- 分析与邮件是 1:1 关系，独立成表支持重跑幂等（`ON CONFLICT (email_id) DO UPDATE`）和模型版本追溯。
- 分析状态（analyzed/failed）与邮件已读语义（`is_read`）职责不同：`is_read` 标记 agent 是否已读取处理，`status` 标记分析流程本身是否成功。

## 4. 变更记录

> 本节只追加不修改；每条记录 = 日期 + 说明 + SQL 操作。

### 2026-08-25 初始建表

创建 `email_accounts`、`emails` 两张表，DDL 见第 2 节。

### 2026-08-25 补充表与字段注释

为两张表补充 `COMMENT ON TABLE / COLUMN` 注释，与第 2 节字段说明表保持一致；执行第 2 节 DDL 时注释会一并写入数据库元数据。

### 2026-08-25 数据访问层迁移至 SQLAlchemy ORM（表结构不变）

数据访问层由手写连接池（psycopg2 `ThreadedConnectionPool`）+ 原生 SQL repository 迁移为 SQLAlchemy 2.x ORM：
引擎与连接池由 `db/engine.py` 的 `create_engine` 统一管理；`Account`/`EmailMessage` 改为声明式 ORM 模型；
`repository/` 改为基于 `Session` 的薄封装（同一账号的邮件入库与断点推进共享一个 Session，由一次 `commit()` 原子提交）。
**本变更不修改任何表结构、约束或列定义**，原有 DDL（含 `(account_id, uid)` 唯一约束）保持不变，幂等写入改为 `pg_insert(...).on_conflict_do_nothing()`。
驱动统一使用 psycopg v3（连接串 `postgresql+psycopg://`），不再兼容 psycopg2。

### 2026-08-28 emails 表新增 is_read 列（支持未读语义）

```sql
ALTER TABLE emails ADD COLUMN is_read BOOLEAN NOT NULL DEFAULT FALSE;
COMMENT ON COLUMN emails.is_read IS '是否已处理（agent 读取后置 TRUE）；FALSE 表示未读';
```

用于标记邮件是否已被 agent 读取处理：sync 落库时默认未读，agent 上下文注入后标记为已读。

### 2026-08-28 新增 email_analyses 表（邮件结构化分析）

```sql
CREATE TABLE email_analyses (
    id                SERIAL PRIMARY KEY,
    email_id          INT  NOT NULL UNIQUE,
    account_id        INT  NOT NULL,
    primary_intent    TEXT NOT NULL,
    intents           JSONB NOT NULL DEFAULT '[]',
    reasoning_summary TEXT NOT NULL DEFAULT '',
    entities          JSONB NOT NULL DEFAULT '{}',
    sentiment         TEXT NOT NULL DEFAULT 'neutral',
    priority          TEXT NOT NULL DEFAULT 'P2',
    suggested_tools   JSONB NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'analyzed',
    error             TEXT,
    model             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

邮件结构化分析表，作为 agent 节点 1（邮件预处理与意图分析）的持久化输出。DDL 与字段注释见 §2.3。

### 2026-08-28 意图分类标准化：枚举常量单一来源 + 注释中文化（无表结构变更）

- 意图/情绪/优先级枚举收敛为代码单一来源（`app/schemas/analysis.py`），prompt 生成与 db 层白名单校验均引用该处。
- `primary_intent`/`intents[].category` 改为白名单强校验：LLM 输出白名单外意图时校验失败 → 分析走 fallback（status=failed）。
- `email_analyses` 的 `primary_intent`/`intents` 列注释补充中文含义，新增「意图分类表」见 §2.4。
- **无任何表结构、约束或列定义变更，DDL 无需执行。**

### 2026-08-29 email_analyses 新增翻译三列（支持检测翻译节点）

分析图新增 `detect_and_translate` 节点（analyze 后按意图条件路由，垃圾/通知类不进该节点），
检测非中文业务邮件的源语言并将主题/正文译为简体中文。`email_analyses` 追加三个可空列：

```sql
ALTER TABLE email_analyses
    ADD COLUMN source_language TEXT,
    ADD COLUMN translated_subject TEXT,
    ADD COLUMN translated_text TEXT;
COMMENT ON COLUMN email_analyses.source_language IS '检测到的源语言 ISO 639-1 代码（detect_and_translate 节点产出，如 en/ja/zh/unknown），中文启发式短路或垃圾邮件跳过翻译时为空';
COMMENT ON COLUMN email_analyses.translated_subject IS '主题中文译文，仅非中文业务邮件非空（detect_and_translate 节点产出）';
COMMENT ON COLUMN email_analyses.translated_text IS '正文中文译文，仅非中文业务邮件非空（detect_and_translate 节点产出）';
```

中文邮件（启发式短路）、垃圾/通知邮件、分析失败路径三列均为 NULL；DDL 与字段注释见 §2.3。

### 2026-08-30 附件接入：新增 email_attachments 表 + email_analyses 新增证据来源列

附件字节不再丢弃/不落库：抓取时上传腾讯云 COS，DB 仅存对象引用。

```sql
CREATE TABLE email_attachments (
    id             SERIAL PRIMARY KEY,
    email_id       INT  NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'document',
    filename       TEXT NOT NULL DEFAULT '',
    content_type   TEXT NOT NULL DEFAULT '',
    disposition    TEXT,
    content_id     TEXT,
    size           INT  NOT NULL DEFAULT 0,
    storage_url    TEXT,
    storage_key    TEXT,
    extracted_text TEXT,
    extracted_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_email_attachments_email_id ON email_attachments(email_id);

ALTER TABLE email_analyses
    ADD COLUMN intent_evidence_source TEXT NOT NULL DEFAULT 'body';
COMMENT ON COLUMN email_analyses.intent_evidence_source IS '主意图的证据来源：body（壳层正文）/ attached_email（.eml 转发邮件附件）/ image（图片视觉识别）/ mixed（多层综合）';
```

- `email_attachments`：一行 = 一个附件的元数据 + COS 引用（`storage_url`/`storage_key`）；
  `extracted_text` 为分析阶段内容提取缓存（.eml 解析文本 / 图片识别文本）。
- `intent_evidence_source`：意图分析新增"证据来源"输出，支撑分层视图判定
  （壳层正文 / 转发邮件 / 图片 / 混合），规则见分析图 system prompt。
- DDL 与字段注释见 §2.3 / §2.4。

### 2026-08-30 新增 RAG 知识库两表：kb_documents / kb_chunks（未接邮件业务）

支撑"回复邮件草稿"的知识库地基（M1 里程碑，仅数据层；与邮件链路零接线）。
前置启用 pgvector 扩展（需 DB 执行权限）：

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE kb_documents (
    id           SERIAL PRIMARY KEY,
    kb_type      TEXT NOT NULL,
    title        TEXT NOT NULL,
    source_type  TEXT NOT NULL DEFAULT 'text',
    source_key   TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE kb_chunks (
    id              SERIAL PRIMARY KEY,
    document_id     INT NOT NULL,
    kb_type         TEXT NOT NULL,
    chunk_index     INT NOT NULL,
    content         TEXT NOT NULL,
    embedding       vector(1536) NOT NULL,
    embedding_model TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX idx_kb_chunks_kb_type ON kb_chunks (kb_type);
```

- `kb_documents`：文档级管理语义——`source_key` 幂等重入库、`content_hash` 变更检测、`status` 软下线。
- `kb_chunks`：检索粒度——1536 维向量 + `embedding_model` 归属（防换模型后向量混空间）；重入库整篇换块。
- 知识类型白名单（faq / sop / compliance）见 §2.8，代码单一来源 `app/schemas/knowledge.py`。
- 可执行脚本：`scripts/kb_schema.sql`（建表）、`scripts/kb_seed.sql`（示例数据，占位向量 embedding_model='seed-dummy-1536'）。
- 向量索引（ivfflat/hnsw）待 M2 embedding 维度定型后再加；ORM/仓储见 `app/db/db.py`（`KbDocument`/`KbChunk`）与 `app/db/repositories.py`。

### 2026-08-30 分析图对接 RAG：新增 email_drafts 表 + 意图枚举扩充（售前/售后）

分析图新增条件草稿分支：主意图命中售前/售后意图映射（单一来源 `app/schemas/draft.py` 的
`DRAFT_CATEGORY_BY_INTENT`）且配置了 embedding 时，draft_presale / draft_aftersale 节点
检索知识库（售前查 faq、售后查 sop）起草回复草稿，落 `email_drafts` 待人工确认；
**本系统不发送邮件**。检索无相关知识时不出草稿（转人工）。

```sql
CREATE TABLE email_drafts (
    id         SERIAL PRIMARY KEY,
    email_id   INT  NOT NULL UNIQUE,
    account_id INT  NOT NULL,
    category   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    sources    JSONB NOT NULL DEFAULT '[]',
    model      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_email_drafts_account_id ON email_drafts(account_id);

COMMENT ON TABLE email_drafts IS '回复草稿表（输出）：待人工确认，本系统不发送邮件';
COMMENT ON COLUMN email_drafts.category IS '草稿类别：presale（售前咨询，检索 faq 知识）/ aftersale（售后问题，检索 sop 知识）';
COMMENT ON COLUMN email_drafts.status IS '确认状态：pending（待人工确认）/ approved（人工确认可用）/ rejected（人工否决）；重生成后重置回 pending';
COMMENT ON COLUMN email_drafts.sources IS '检索依据，JSONB 数组：[{"document_id":1,"title":"...","distance":0.32,"snippet":"..."}]，人工核对草稿事实用';
```

- 意图枚举扩充（无表结构变更，`primary_intent` 白名单自动跟随）：ToC 新增
  `pre_sales_consult`（售前咨询）、`after_sales_consult`（售后咨询），见 §2.5。
- 草稿状态流转：pending →（人工）approved / rejected；重生成整体覆盖并重置 pending。
- DDL 与字段注释见 §2.9；ORM/仓储见 `app/db/db.py`（`EmailDraft`）与
  `app/db/repositories.py`（`EmailDraftRepository`）；确认入口 `draft_list` / `draft_review` CLI。

### 2026-08-30 草稿链补全：合规红线全量注入 + 检索命中带文档标题（无表结构变更）

- 合规红线（`kb_type=compliance`）接入草稿链：`EmailCoordinator._load_compliance_rules`
  经 `KbChunkRepository.list_kb_chunk_by_kb_type`（非向量全量，仅 active 文档）读出红线块
  文本，经初始 state `compliance_rules` 注入草稿 prompt（「红线规则」段，优先级最高，
  总字符预算 `DRAFT_COMPLIANCE_MAX_CHARS`）；读取失败降级为空列表不拖垮分析主链。
  新增仓储方法无 DDL 变更。
- 检索命中带文档标题：`list_kb_chunk_by_similarity` 改为 join `kb_documents`，
  返回行 `KbChunkSimilarityRow(chunk, document_title, distance)`；
  `email_drafts.sources` 每项增加 `title` 键（`RetrievedChunk.document_title` 透传），
  sources 记录形状更新为 `[{document_id, title, distance, snippet}]`（§2.9 注释同步）。
- 可执行脚本：`scripts/draft_schema.sql`（email_drafts 建表，与 §2.9 严格 1:1）。
  已按 §2.9 旧注释建表的库如需对齐注释可重跑 §2.9 的 COMMENT ON 语句（可选，无数据变更）。
