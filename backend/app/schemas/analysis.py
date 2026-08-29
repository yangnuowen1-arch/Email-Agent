"""邮件结构化分析的 Pydantic 输出 Schema。

供 LLM with_structured_output 使用，确保模型输出可直接写入 email_analyses 表。

意图/情绪/优先级枚举的唯一出处：db 层白名单校验与 prompt 生成均引用本模块常量，
新增意图必须先在此扩展。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 意图分类常量（单一来源，与 docs/db-schema.md 意图分类表 1:1）
# ---------------------------------------------------------------------------

#: 意图 key 常量（供路由/分发等下游按常量引用，禁止散落裸字符串）
INTENT_CANCEL_ORDER = "cancel_order"
INTENT_REFUND_REQUEST = "refund_request"
INTENT_ORDER_STATUS_QUERY = "order_status_query"
INTENT_INVOICE_QUERY = "invoice_query"
INTENT_MEETING_REQUEST = "meeting_request"
INTENT_COMPLAINT = "complaint"
INTENT_SPAM_OR_NOTICE = "spam_or_notice"
INTENT_OTHER = "other"
INTENT_CONTRACT = "contract"
INTENT_PAYMENT = "payment"
INTENT_PARTNERSHIP = "partnership"
INTENT_TECHNICAL_ISSUE = "technical_issue"
INTENT_ACCOUNT_MANAGEMENT = "account_management"

#: ToC（消费者场景）意图：英文标识 → 中文含义
CONSUMER_INTENTS: dict[str, str] = {
    INTENT_CANCEL_ORDER: "取消订单/退订服务",
    INTENT_REFUND_REQUEST: "退款申请",
    INTENT_ORDER_STATUS_QUERY: "订单状态查询/物流追踪",
    INTENT_INVOICE_QUERY: "发票/账单查询",
    INTENT_MEETING_REQUEST: "会议/日程请求",
    INTENT_COMPLAINT: "投诉/不满表达",
    INTENT_SPAM_OR_NOTICE: "垃圾邮件/系统通知/广告",
    INTENT_OTHER: "无法归类的消费者意图",
}

#: ToB（企业场景）意图：英文标识 → 中文含义
BUSINESS_INTENTS: dict[str, str] = {
    INTENT_CONTRACT: "合同/协议相关",
    INTENT_PAYMENT: "付款/结算相关",
    INTENT_PARTNERSHIP: "合作/商务洽谈",
    INTENT_TECHNICAL_ISSUE: "技术问题/故障报告",
    INTENT_ACCOUNT_MANAGEMENT: "账号/权限管理",
}

#: 全部意图（含兜底），英文标识 → 中文含义
ALL_INTENTS: dict[str, str] = {**CONSUMER_INTENTS, **BUSINESS_INTENTS}

#: 无法判定意图时的兜底标识（fallback 落库使用）
UNKNOWN_INTENT = "unknown_manual_review"
ALL_INTENTS[UNKNOWN_INTENT] = "无法判定，待人工复核"

#: 意图 Literal（含兜底），LLM 输出白名单外意图时校验失败 → 走 fallback
INTENT_LITERAL = Literal[
    "cancel_order",
    "refund_request",
    "order_status_query",
    "invoice_query",
    "meeting_request",
    "complaint",
    "spam_or_notice",
    "other",
    "contract",
    "payment",
    "partnership",
    "technical_issue",
    "account_management",
    "unknown_manual_review",
]

#: 发件人情绪取值（英文标识 → 中文含义见 db-schema.md §2.3）
SENTIMENTS: tuple[str, ...] = ("positive", "neutral", "negative", "angry", "urgent")
SENTIMENT_LITERAL = Literal["positive", "neutral", "negative", "angry", "urgent"]

#: 处理优先级取值
PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")
PRIORITY_LITERAL = Literal["P0", "P1", "P2", "P3"]


class IntentDetail(BaseModel):
    """单条意图详情。"""

    category: INTENT_LITERAL = Field(
        description="意图分类标识（白名单枚举），取值含中文含义见 ALL_INTENTS"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="置信度 0.0-1.0")
    reasoning: str = Field(description="推导该意图的具体依据")


class EmailAnalysisOutput(BaseModel):
    """LLM 邮件结构化分析的完整输出 schema。"""

    primary_intent: INTENT_LITERAL = Field(
        description="核心主意图（白名单枚举），用于后续路由；无法判定时为 unknown_manual_review"
    )
    intents: list[IntentDetail] = Field(min_length=1, description="多意图列表")
    reasoning_summary: str = Field(default="", description="AI 全局综合判定总结")
    entities: dict[str, Any] = Field(default_factory=dict, description="提取的关键业务实体")
    sentiment: SENTIMENT_LITERAL = Field(default="neutral", description="发件人情绪")
    priority: PRIORITY_LITERAL = Field(default="P2", description="处理优先级")
    suggested_tools: list[str] = Field(default_factory=list, description="建议调用的 Tool 名列表")
