"""EmailDraft 模型单元测试：白名单校验与默认值。"""

from __future__ import annotations

import pytest

from app.db.db import _VALID_DRAFT_CATEGORIES, _VALID_DRAFT_STATUSES, EmailDraft
from app.schemas.draft import ALL_DRAFT_CATEGORIES, ALL_DRAFT_STATUSES


def _make_draft(**overrides):
    fields = {
        "email_id": 1,
        "account_id": 1,
        "category": "presale",
        "subject": "Re: 产品价格咨询",
        "body": "您好，感谢来信。关于价格问题回复如下……",
    }
    return EmailDraft(**{**fields, **overrides})


def test_email_draft_construction_defaults() -> None:
    entity = _make_draft()
    assert entity.email_id == 1
    assert entity.account_id == 1
    assert entity.category == "presale"
    assert entity.status == "pending"
    assert entity.sources == []
    assert entity.model is None


def test_email_draft_sources_default_not_shared() -> None:
    """默认 sources 不得在实例间共享同一可变列表。"""
    first, second = _make_draft(), _make_draft()
    first.sources.append({"document_id": 1})
    assert second.sources == []


def test_email_draft_all_fields() -> None:
    entity = EmailDraft(
        email_id=42,
        account_id=3,
        category="aftersale",
        status="approved",
        subject="Re: 退款进度",
        body="您好，您的退款正在处理中。",
        sources=[{"document_id": 7, "distance": 0.31, "snippet": "退款政策……"}],
        model="gpt-4o-mini",
    )
    assert entity.category == "aftersale"
    assert entity.status == "approved"
    assert entity.sources[0]["document_id"] == 7
    assert entity.model == "gpt-4o-mini"


def test_valid_categories_match_schema_constants() -> None:
    assert set(ALL_DRAFT_CATEGORIES) == _VALID_DRAFT_CATEGORIES


def test_valid_statuses_match_schema_constants() -> None:
    assert set(ALL_DRAFT_STATUSES) == _VALID_DRAFT_STATUSES


@pytest.mark.parametrize("category", sorted(_VALID_DRAFT_CATEGORIES))
def test_valid_categories_accepted(category: str) -> None:
    assert _make_draft(category=category).category == category


@pytest.mark.parametrize("status", sorted(_VALID_DRAFT_STATUSES))
def test_valid_statuses_accepted(status: str) -> None:
    assert _make_draft(status=status).status == status


def test_invalid_category_raises() -> None:
    with pytest.raises(ValueError, match="category must be one of"):
        _make_draft(category="consult")


def test_invalid_status_raises() -> None:
    with pytest.raises(ValueError, match="status must be one of"):
        _make_draft(status="sent")


def test_invalid_email_id_raises() -> None:
    with pytest.raises(ValueError, match="email_id must be positive"):
        _make_draft(email_id=0)


def test_invalid_account_id_raises() -> None:
    with pytest.raises(ValueError, match="account_id must be positive"):
        _make_draft(account_id=-1)


def test_non_int_email_id_raises() -> None:
    with pytest.raises(ValueError, match="email_id must be positive"):
        _make_draft(email_id="1")


def test_empty_subject_raises() -> None:
    with pytest.raises(ValueError, match="subject must be non-empty"):
        _make_draft(subject="")


def test_blank_subject_raises() -> None:
    with pytest.raises(ValueError, match="subject must be non-empty"):
        _make_draft(subject="   ")


def test_empty_body_raises() -> None:
    with pytest.raises(ValueError, match="body must be non-empty"):
        _make_draft(body="")
