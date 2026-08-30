"""知识库领域常量：kb_type / source_type / status / 向量维度。

RAG 知识库（支撑回复邮件草稿）的枚举唯一出处：db 层白名单校验、
种子数据与（M2 的）检索管线均引用本模块常量，
新增知识类型必须先在此扩展，再同步 docs/db-schema.md §2.8 知识类型表。
"""

from __future__ import annotations

from typing import Literal

# ---------------------------------------------------------------------------
# 知识类型（单一来源，与 docs/db-schema.md §2.8 知识类型表 1:1）
# ---------------------------------------------------------------------------

#: 知识类型 key 常量（供路由/检索过滤等下游按常量引用，禁止散落裸字符串）
KB_TYPE_FAQ = "faq"
KB_TYPE_SOP = "sop"
KB_TYPE_COMPLIANCE = "compliance"

#: 全部知识类型：英文标识 → 中文含义与业务用途
ALL_KB_TYPES: dict[str, str] = {
    KB_TYPE_FAQ: "业务与产品 FAQ（报价/交付/技术问答，防止模型杜撰）",
    KB_TYPE_SOP: "沟通 SOP 与语气（称呼/落款/场景模板，保持对外沟通一致）",
    KB_TYPE_COMPLIANCE: "合规与红线规则（不可承诺事项/转人工触发词，草稿不越线）",
}

#: 知识类型 Literal（ORM 写入白名单）
KB_TYPE_LITERAL = Literal["faq", "sop", "compliance"]

# ---------------------------------------------------------------------------
# 来源形态（kb_documents.source_type 白名单）
# ---------------------------------------------------------------------------

#: 来源形态 key 常量
KB_SOURCE_TYPE_MAIL = "mail"  # 从邮件/附件提取
KB_SOURCE_TYPE_FILE = "file"  # 文件导入（PDF/Markdown 等）
KB_SOURCE_TYPE_TEXT = "text"  # 手工维护文本

#: 全部来源形态：英文标识 → 中文含义
ALL_KB_SOURCE_TYPES: dict[str, str] = {
    KB_SOURCE_TYPE_MAIL: "邮件提取",
    KB_SOURCE_TYPE_FILE: "文件导入",
    KB_SOURCE_TYPE_TEXT: "手工维护",
}

#: 来源形态 Literal（ORM 写入白名单）
KB_SOURCE_TYPE_LITERAL = Literal["mail", "file", "text"]

# ---------------------------------------------------------------------------
# 知识状态（kb_documents.status 白名单）
# ---------------------------------------------------------------------------

#: 生效：参与检索
KB_STATUS_ACTIVE = "active"
#: 归档下线：仅留档审计，检索不再命中
KB_STATUS_ARCHIVED = "archived"

#: 全部知识状态
ALL_KB_STATUSES: tuple[str, ...] = (KB_STATUS_ACTIVE, KB_STATUS_ARCHIVED)

#: 知识状态 Literal（ORM 写入白名单）
KB_STATUS_LITERAL = Literal["active", "archived"]

# ---------------------------------------------------------------------------
# 向量维度（与 docs/db-schema.md §2.7 的 vector(1536) 严格对齐）
# ---------------------------------------------------------------------------

#: embedding 向量维度：DDL、ORM、（M2 的）配置校验三处引用同一常量；
#: 换 embedding 模型时先改文档 §4 变更记录，再改此处与列定义
KB_EMBEDDING_DIMENSIONS = 1536
