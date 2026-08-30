"""回复草稿领域常量：category / status / 意图→草稿分支映射。

草稿链路的枚举唯一出处：图路由（DRAFT_CATEGORY_BY_INTENT）、
db 层白名单校验与 prompt 均引用本模块常量，
新增草稿类别必须先在此扩展，再同步 docs/db-schema.md §2.9。

明确边界：本系统只产出待确认草稿（email_drafts），不自动发送邮件。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.analysis import (
    INTENT_AFTER_SALES_CONSULT,
    INTENT_CANCEL_ORDER,
    INTENT_COMPLAINT,
    INTENT_INVOICE_QUERY,
    INTENT_ORDER_STATUS_QUERY,
    INTENT_PRE_SALES_CONSULT,
    INTENT_REFUND_REQUEST,
)

# ---------------------------------------------------------------------------
# 草稿类别（单一来源，与 docs/db-schema.md §2.9 1:1）
# ---------------------------------------------------------------------------

#: 草稿类别 key 常量
DRAFT_CATEGORY_PRE_SALE = "presale"  # 售前：产品/价格/优惠等购买前咨询
DRAFT_CATEGORY_AFTER_SALE = "aftersale"  # 售后：退换货/保修/物流等售后问题

#: 全部草稿类别
ALL_DRAFT_CATEGORIES: tuple[str, ...] = (DRAFT_CATEGORY_PRE_SALE, DRAFT_CATEGORY_AFTER_SALE)

#: 草稿类别 Literal（ORM 写入白名单）
DRAFT_CATEGORY_LITERAL = Literal["presale", "aftersale"]

# ---------------------------------------------------------------------------
# 草稿状态（email_drafts.status 白名单，人工确认流）
# ---------------------------------------------------------------------------

#: 待人工确认（生成后的初始状态）
DRAFT_STATUS_PENDING = "pending"
#: 人工确认可用（发送动作在系统之外由人工完成）
DRAFT_STATUS_APPROVED = "approved"
#: 人工否决
DRAFT_STATUS_REJECTED = "rejected"

#: 全部草稿状态
ALL_DRAFT_STATUSES: tuple[str, ...] = (
    DRAFT_STATUS_PENDING,
    DRAFT_STATUS_APPROVED,
    DRAFT_STATUS_REJECTED,
)

#: 草稿状态 Literal（ORM 写入白名单）
DRAFT_STATUS_LITERAL = Literal["pending", "approved", "rejected"]

# ---------------------------------------------------------------------------
# 草稿降级原因（draft_skipped_reason 白名单，图内降级路径唯一出处）
# ---------------------------------------------------------------------------

#: 意图与节点类别不匹配（路由错配防御，检索前早退）
DRAFT_SKIP_INTENT_MISMATCH = "intent_category_mismatch"
#: 主题与正文均为空，无检索 query 可组（检索前早退）
DRAFT_SKIP_EMPTY_QUERY = "empty_query"
#: 知识库向量检索失败（embedding 网关不可用等非 LLM IO）
DRAFT_SKIP_RETRIEVAL_FAILED = "retrieval_failed"
#: 无命中或最近余弦距离超 DRAFT_MAX_COSINE_DISTANCE，视为无相关知识
DRAFT_SKIP_NO_KNOWLEDGE = "no_relevant_knowledge"
#: LLM 起草失败（超时/网络/解析失败/返回 None）
DRAFT_SKIP_GENERATION_FAILED = "generation_failed"

#: 草稿降级原因 Literal（state 注解与测试断言依据）
DRAFT_SKIPPED_REASON_LITERAL = Literal[
    "intent_category_mismatch",
    "empty_query",
    "retrieval_failed",
    "no_relevant_knowledge",
    "generation_failed",
]

# ---------------------------------------------------------------------------
# 意图 → 草稿分支映射（路由唯一出处）
# ---------------------------------------------------------------------------

#: 售前分支触发的意图集合
PRE_SALE_INTENTS: frozenset[str] = frozenset({INTENT_PRE_SALES_CONSULT})

#: 售后分支触发的意图集合（含退单/退款/物流/发票/投诉等既有意图）
AFTER_SALE_INTENTS: frozenset[str] = frozenset(
    {
        INTENT_CANCEL_ORDER,
        INTENT_REFUND_REQUEST,
        INTENT_ORDER_STATUS_QUERY,
        INTENT_INVOICE_QUERY,
        INTENT_COMPLAINT,
        INTENT_AFTER_SALES_CONSULT,
    }
)

#: 主意图 → 草稿类别：不在映射内的意图不出草稿（ToB / meeting / other / spam / unknown）
DRAFT_CATEGORY_BY_INTENT: dict[str, str] = {
    intent: DRAFT_CATEGORY_PRE_SALE for intent in PRE_SALE_INTENTS
} | {intent: DRAFT_CATEGORY_AFTER_SALE for intent in AFTER_SALE_INTENTS}


class EmailDraftOutput(BaseModel):
    """LLM 回复草稿输出 schema：主题与正文，落 email_drafts 待人工确认。"""

    subject: str = Field(
        min_length=1,
        max_length=200,
        description="回复邮件主题；默认在原主题前加 Re: 前缀",
    )
    body: str = Field(
        min_length=1,
        max_length=4000,
        description="回复邮件正文；使用客户来信语言，仅基于知识库摘录与礼貌话术",
    )
