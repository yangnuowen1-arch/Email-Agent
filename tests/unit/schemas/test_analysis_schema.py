"""分析 Schema 单元测试：意图/情绪/优先级枚举常量与 Literal 强校验。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import analysis
from app.schemas.analysis import (
    ALL_INTENTS,
    INTENT_LITERAL,
    PRIORITIES,
    PRIORITY_LITERAL,
    SENTIMENT_LITERAL,
    SENTIMENTS,
    UNKNOWN_INTENT,
    EmailAnalysisOutput,
    IntentDetail,
)


def test_intent_literal_matches_all_intents() -> None:
    """Literal 与常量字典键集合同步，防止漂移。"""
    literal_values = set(INTENT_LITERAL.__args__)
    assert literal_values == set(ALL_INTENTS)


def test_unknown_intent_is_in_all_intents() -> None:
    assert UNKNOWN_INTENT in ALL_INTENTS
    assert ALL_INTENTS[UNKNOWN_INTENT]  # 中文含义非空


def test_unknown_intent_is_in_literal() -> None:
    assert UNKNOWN_INTENT in INTENT_LITERAL.__args__


def test_intent_key_constants_cover_all_intents() -> None:
    """INTENT_* 常量与 ALL_INTENTS 键集合一一对应，防止新增意图时漏建常量。"""
    constant_values = {
        value
        for name, value in vars(analysis).items()
        if name.startswith("INTENT_") and isinstance(value, str)
    }
    assert constant_values == set(ALL_INTENTS) - {UNKNOWN_INTENT}


def test_every_intent_has_chinese_meaning() -> None:
    for key, meaning in ALL_INTENTS.items():
        assert meaning, f"intent {key!r} 缺少中文含义"


@pytest.mark.parametrize("category", sorted(ALL_INTENTS))
def test_valid_category_accepted(category: str) -> None:
    detail = IntentDetail(category=category, confidence=0.9, reasoning="test")
    assert detail.category == category


@pytest.mark.parametrize("primary_intent", sorted(ALL_INTENTS))
def test_valid_primary_intent_accepted(primary_intent: str) -> None:
    output = EmailAnalysisOutput(
        primary_intent=primary_intent,
        intents=[IntentDetail(category=primary_intent, confidence=0.9, reasoning="test")],
    )
    assert output.primary_intent == primary_intent


@pytest.mark.parametrize("category", ["x", "cancel", "refund", ""])
def test_invalid_category_raises(category: str) -> None:
    with pytest.raises(ValidationError):
        IntentDetail(category=category, confidence=0.9, reasoning="test")


def test_invalid_primary_intent_raises() -> None:
    with pytest.raises(ValidationError):
        EmailAnalysisOutput(
            primary_intent="bogus",
            intents=[IntentDetail(category="cancel_order", confidence=0.9, reasoning="t")],
        )


@pytest.mark.parametrize("sentiment", SENTIMENTS)
def test_valid_sentiment_accepted(sentiment: str) -> None:
    assert sentiment in SENTIMENT_LITERAL.__args__
    output = EmailAnalysisOutput(
        primary_intent="cancel_order",
        intents=[IntentDetail(category="cancel_order", confidence=0.9, reasoning="t")],
        sentiment=sentiment,
    )
    assert output.sentiment == sentiment


def test_invalid_sentiment_raises() -> None:
    with pytest.raises(ValidationError):
        EmailAnalysisOutput(
            primary_intent="cancel_order",
            intents=[IntentDetail(category="cancel_order", confidence=0.9, reasoning="t")],
            sentiment="unknown",
        )


@pytest.mark.parametrize("priority", PRIORITIES)
def test_valid_priority_accepted(priority: str) -> None:
    assert priority in PRIORITY_LITERAL.__args__
    output = EmailAnalysisOutput(
        primary_intent="cancel_order",
        intents=[IntentDetail(category="cancel_order", confidence=0.9, reasoning="t")],
        priority=priority,
    )
    assert output.priority == priority


def test_invalid_priority_raises() -> None:
    with pytest.raises(ValidationError):
        EmailAnalysisOutput(
            primary_intent="cancel_order",
            intents=[IntentDetail(category="cancel_order", confidence=0.9, reasoning="t")],
            priority="P99",
        )
