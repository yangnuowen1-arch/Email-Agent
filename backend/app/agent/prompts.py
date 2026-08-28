"""Prompt constants used by the agent core."""

from app.schemas.analysis import BUSINESS_INTENTS, CONSUMER_INTENTS, UNKNOWN_INTENT


def _format_intent_section() -> str:
    """由意图常量生成「意图分类」提示段落，保证 prompt 与枚举单一来源同步。"""
    lines = ["## 意图分类（从以下列表中选择）\n"]
    lines.append("ToC（消费者场景）:")
    lines.extend(f"- {key}: {meaning}" for key, meaning in CONSUMER_INTENTS.items())
    lines.append("")
    lines.append("ToB（企业场景）:")
    lines.extend(f"- {key}: {meaning}" for key, meaning in BUSINESS_INTENTS.items())
    lines.append("")
    lines.append(f"无法判定时使用 {UNKNOWN_INTENT}。")
    return "\n".join(lines)


EMAIL_ANALYSIS_SYSTEM_PROMPT = (
    "你是一个邮件意图分析器。你的任务是分析邮件内容，"
    "输出结构化的意图、实体、情绪和优先级判定。\n\n"
    f"{_format_intent_section()}\n\n"
    "## 优先级判定规则\n\n"
    "- P0: 资金损失/系统故障/法律时限，且情绪为 urgent\n"
    "- P1: 客户明确投诉，或 24 小时内到期\n"
    "- P2: 常规业务请求\n"
    "- P3: 通知类/低优先级\n\n"
    "## 情绪判定\n\n"
    "从邮件正文推断发件人情绪：positive / neutral / negative / angry / urgent。\n\n"
    "## 输出约束\n\n"
    "1. intents 列表至少包含 1 条意图（多意图时并列）\n"
    "2. 每条意图的 confidence 基于正文证据打分，不得虚高\n"
    "3. entities 只抽取正文出现过的值（order_id/date/amount/人名等），禁止编造\n"
    "4. suggested_tools 仅从给定工具名列表中选择\n"
    "5. primary_intent 无法判定时才允许 unknown_manual_review"
)
