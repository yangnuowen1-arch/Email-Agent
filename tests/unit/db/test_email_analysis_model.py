"""EmailAnalysis 模型单元测试：白名单校验与默认值。"""

from __future__ import annotations

import pytest

from app.db.db import (
    _VALID_INTENTS,
    _VALID_PRIORITIES,
    _VALID_SENTIMENTS,
    EmailAnalysis,
)
from app.schemas.analysis import ALL_INTENTS, UNKNOWN_INTENT


def test_email_analysis_construction_defaults() -> None:
    entity = EmailAnalysis(email_id=1, account_id=1)
    assert entity.email_id == 1
    assert entity.account_id == 1
    assert entity.primary_intent == "unknown_manual_review"
    assert entity.intents == []
    assert entity.entities == {}
    assert entity.sentiment == "neutral"
    assert entity.priority == "P2"
    assert entity.suggested_tools == []
    assert entity.status == "analyzed"
    assert entity.error is None
    assert entity.model is None


def test_email_analysis_all_fields() -> None:
    entity = EmailAnalysis(
        email_id=42,
        account_id=3,
        primary_intent="cancel_order",
        intents=[{"category": "cancel_order", "confidence": 0.9, "reasoning": "test"}],
        reasoning_summary="用户要求取消订单",
        entities={"order_id": "ORD-123"},
        sentiment="negative",
        priority="P1",
        suggested_tools=["summarize_emails"],
        status="analyzed",
        model="gpt-4o-mini",
    )
    assert entity.primary_intent == "cancel_order"
    assert entity.priority == "P1"
    assert entity.model == "gpt-4o-mini"


@pytest.mark.parametrize("sentiment", sorted(_VALID_SENTIMENTS))
def test_valid_sentiments(sentiment: str) -> None:
    entity = EmailAnalysis(email_id=1, account_id=1, sentiment=sentiment)
    assert entity.sentiment == sentiment


@pytest.mark.parametrize("priority", sorted(_VALID_PRIORITIES))
def test_valid_priorities(priority: str) -> None:
    entity = EmailAnalysis(email_id=1, account_id=1, priority=priority)
    assert entity.priority == priority


def test_invalid_sentiment_raises() -> None:
    with pytest.raises(ValueError, match="sentiment must be one of"):
        EmailAnalysis(email_id=1, account_id=1, sentiment="unknown")


def test_invalid_priority_raises() -> None:
    with pytest.raises(ValueError, match="priority must be one of"):
        EmailAnalysis(email_id=1, account_id=1, priority="P99")


@pytest.mark.parametrize("intent", sorted(_VALID_INTENTS))
def test_valid_intents(intent: str) -> None:
    entity = EmailAnalysis(email_id=1, account_id=1, primary_intent=intent)
    assert entity.primary_intent == intent


def test_invalid_intent_raises() -> None:
    with pytest.raises(ValueError, match="primary_intent must be one of"):
        EmailAnalysis(email_id=1, account_id=1, primary_intent="bogus")


def test_unknown_intent_is_valid_and_in_all_intents() -> None:
    assert UNKNOWN_INTENT in ALL_INTENTS
    entity = EmailAnalysis(email_id=1, account_id=1)
    assert entity.primary_intent == UNKNOWN_INTENT


def test_invalid_status_raises() -> None:
    with pytest.raises(ValueError, match="status must be one of"):
        EmailAnalysis(email_id=1, account_id=1, status="pending")


def test_failed_requires_error() -> None:
    with pytest.raises(ValueError, match="status='failed' requires non-empty error"):
        EmailAnalysis(email_id=1, account_id=1, status="failed", error=None)


def test_failed_with_error_succeeds() -> None:
    entity = EmailAnalysis(email_id=1, account_id=1, status="failed", error="LLM timeout")
    assert entity.status == "failed"
    assert entity.error == "LLM timeout"


def test_invalid_email_id_raises() -> None:
    with pytest.raises(ValueError, match="email_id must be positive"):
        EmailAnalysis(email_id=0, account_id=1)


def test_invalid_account_id_raises() -> None:
    with pytest.raises(ValueError, match="account_id must be positive"):
        EmailAnalysis(email_id=1, account_id=-1)


def test_intent_evidence_source_defaults_to_body() -> None:
    entity = EmailAnalysis(email_id=1, account_id=1)
    assert entity.intent_evidence_source == "body"


@pytest.mark.parametrize("source", ["body", "attached_email", "image", "mixed"])
def test_valid_intent_evidence_sources(source: str) -> None:
    entity = EmailAnalysis(email_id=1, account_id=1, intent_evidence_source=source)
    assert entity.intent_evidence_source == source


def test_invalid_intent_evidence_source_raises() -> None:
    with pytest.raises(ValueError, match="intent_evidence_source must be one of"):
        EmailAnalysis(email_id=1, account_id=1, intent_evidence_source="screenshot")
