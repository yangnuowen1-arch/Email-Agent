"""Prompt constants used by the agent core."""

from app.schemas.analysis import (
    BUSINESS_INTENTS,
    CONSUMER_INTENTS,
    INTENT_REFUND_REQUEST,
    UNKNOWN_INTENT,
)


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
    "你是一个邮件意图分析器。分析邮件内容，必须以 JSON 格式输出以下字段：\n"
    "primary_intent (字符串), intents (数组，每项含 category、confidence、reasoning), "
    "reasoning_summary (字符串), entities (字典), "
    "sentiment (positive/neutral/negative/angry/urgent), "
    "priority (P0/P1/P2/P3), suggested_tools (字符串列表)。\n\n"
    "输出示例：\n"
    f'{{"primary_intent": "{INTENT_REFUND_REQUEST}", '
    f'"intents": [{{"category": "{INTENT_REFUND_REQUEST}", "confidence": 0.95, '
    f'"reasoning": "用户明确要求为订单 ORD-123 退款"}}], '
    '"reasoning_summary": "用户要求退款", "entities": {"order_id": "ORD-123"}, '
    '"sentiment": "angry", "priority": "P1", "suggested_tools": ["refund_tool"]}\n\n'
    f"{_format_intent_section()}\n\n"
    "## 优先级判定规则\n\n"
    "- P0: 资金损失/系统故障/法律时限，且情绪为 urgent\n"
    "- P1: 客户明确投诉，或 24 小时内到期\n"
    "- P2: 常规业务请求\n"
    "- P3: 通知类/低优先级\n\n"
    "## 情绪判定\n\n"
    "从正文推断：positive / neutral / negative / angry / urgent\n\n"
    "## 附加约束\n\n"
    "1. intents 至少包含 1 条，confidence 基于证据，不得虚高。\n"
    "2. entities 只抽取正文出现过的值，禁止编造。\n"
    "3. suggested_tools 从可用工具列表中选择（请参阅函数定义）。\n"
    f"4. primary_intent 必须输出，无法判定时使用 '{UNKNOWN_INTENT}'。"
)
