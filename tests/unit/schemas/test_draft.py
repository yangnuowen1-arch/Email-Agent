"""草稿领域常量单元测试：category / status 白名单、意图→草稿类别映射与输出 schema。"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from app.schemas.analysis import (
    INTENT_AFTER_SALES_CONSULT,
    INTENT_CANCEL_ORDER,
    INTENT_COMPLAINT,
    INTENT_CONTRACT,
    INTENT_INVOICE_QUERY,
    INTENT_MEETING_REQUEST,
    INTENT_ORDER_STATUS_QUERY,
    INTENT_OTHER,
    INTENT_PAYMENT,
    INTENT_PRE_SALES_CONSULT,
    INTENT_REFUND_REQUEST,
    INTENT_SPAM_OR_NOTICE,
    UNKNOWN_INTENT,
)
from app.schemas.draft import (
    AFTER_SALE_INTENTS,
    ALL_DRAFT_CATEGORIES,
    ALL_DRAFT_STATUSES,
    DRAFT_CATEGORY_AFTER_SALE,
    DRAFT_CATEGORY_BY_INTENT,
    DRAFT_CATEGORY_LITERAL,
    DRAFT_CATEGORY_PRE_SALE,
    DRAFT_STATUS_LITERAL,
    DRAFT_STATUS_PENDING,
    EmailDraftOutput,
)

# ---------------------------------------------------------------------------
# 白名单常量
# ---------------------------------------------------------------------------


def test_all_draft_categories() -> None:
    assert set(ALL_DRAFT_CATEGORIES) == {"presale", "aftersale"}
    assert get_args(DRAFT_CATEGORY_LITERAL) == ALL_DRAFT_CATEGORIES


def test_all_draft_statuses() -> None:
    assert set(ALL_DRAFT_STATUSES) == {"pending", "approved", "rejected"}
    assert get_args(DRAFT_STATUS_LITERAL) == ALL_DRAFT_STATUSES


def test_pending_is_initial_status() -> None:
    assert DRAFT_STATUS_PENDING == "pending"


# ---------------------------------------------------------------------------
# 意图 → 草稿类别映射（路由唯一出处）
# ---------------------------------------------------------------------------


def test_pre_sale_intent_maps_to_presale() -> None:
    assert DRAFT_CATEGORY_BY_INTENT[INTENT_PRE_SALES_CONSULT] == DRAFT_CATEGORY_PRE_SALE


@pytest.mark.parametrize(
    "intent",
    [
        INTENT_CANCEL_ORDER,
        INTENT_REFUND_REQUEST,
        INTENT_ORDER_STATUS_QUERY,
        INTENT_INVOICE_QUERY,
        INTENT_COMPLAINT,
        INTENT_AFTER_SALES_CONSULT,
    ],
)
def test_after_sale_intents_map_to_aftersale(intent: str) -> None:
    assert DRAFT_CATEGORY_BY_INTENT[intent] == DRAFT_CATEGORY_AFTER_SALE


def test_mapping_covers_exactly_the_two_intent_sets() -> None:
    assert set(DRAFT_CATEGORY_BY_INTENT) == set(AFTER_SALE_INTENTS) | {INTENT_PRE_SALES_CONSULT}
    assert AFTER_SALE_INTENTS.isdisjoint({INTENT_PRE_SALES_CONSULT})


def test_non_draft_intents_not_mapped() -> None:
    """ToB / 通知 / other / unknown 等意图不出草稿，不得出现在映射里。"""
    unmapped = (
        UNKNOWN_INTENT,
        INTENT_SPAM_OR_NOTICE,
        INTENT_MEETING_REQUEST,
        INTENT_OTHER,
        INTENT_CONTRACT,
        INTENT_PAYMENT,
    )
    for intent in unmapped:
        assert intent not in DRAFT_CATEGORY_BY_INTENT


# ---------------------------------------------------------------------------
# EmailDraftOutput（草稿节点结构化输出 schema）
# ---------------------------------------------------------------------------


def test_email_draft_output_valid() -> None:
    output = EmailDraftOutput(subject="Re: 关于订单的咨询", body="您好，感谢来信。")
    assert output.subject == "Re: 关于订单的咨询"
    assert output.body == "您好，感谢来信。"


def test_email_draft_output_missing_body_rejected() -> None:
    with pytest.raises(ValidationError):
        EmailDraftOutput(subject="Re: 咨询")  # type: ignore[call-arg]


def test_email_draft_output_empty_subject_rejected() -> None:
    with pytest.raises(ValidationError):
        EmailDraftOutput(subject="", body="正文")


def test_email_draft_output_empty_body_rejected() -> None:
    with pytest.raises(ValidationError):
        EmailDraftOutput(subject="Re: 咨询", body="")


def test_email_draft_output_subject_over_max_length_rejected() -> None:
    with pytest.raises(ValidationError):
        EmailDraftOutput(subject="标" * 201, body="正文")


def test_email_draft_output_body_over_max_length_rejected() -> None:
    with pytest.raises(ValidationError):
        EmailDraftOutput(subject="Re: 咨询", body="正" * 4001)
