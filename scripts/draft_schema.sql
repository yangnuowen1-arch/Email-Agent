-- email_drafts 建表脚本（与 docs/db-schema.md §2.9 严格 1:1）
-- 对应变更记录：2026-08-30 分析图对接 RAG：新增 email_drafts 表 + 意图枚举扩充
-- 幂等语义：UNIQUE(email_id) 支撑 ON CONFLICT DO UPDATE（重生成整体覆盖、status 重置 pending）
-- 无物理外键约束（逻辑外键 email_id → emails.id，与既有表一致）

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
