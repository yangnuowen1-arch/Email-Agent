"""services.parsing.parse_email 单元测试：入参 RawEmail、出参 EmailData，零 app.db 依赖。"""

from __future__ import annotations

import pytest

from app.schemas import EmailData, RawEmail
from app.services.parsing import parse_email

_VALID_RAW = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: bob@example.com\r\n"
    b"Subject: Test Subject\r\n"
    b"Message-ID: <test123@host>\r\n"
    b"Date: Tue, 01 Jan 2024 10:00:00 +0000\r\n"
    b"\r\n"
    b"Hello body\r\n"
)


def test_parse_email_returns_email_data_with_expected_fields():
    result = parse_email(RawEmail(account_id=1, uid=10, raw=_VALID_RAW))

    assert isinstance(result, EmailData)
    assert result.account_id == 1
    assert result.uid == 10
    assert result.subject == "Test Subject"
    assert result.sender == "alice@example.com"
    assert result.recipients == ["bob@example.com"]
    assert result.message_id == "<test123@host>"
    assert result.text_body == "Hello body\r\n"
    assert result.sent_at is not None


def test_parse_email_empty_raw_returns_defaults():
    result = parse_email(RawEmail(account_id=1, uid=0, raw=b""))

    assert isinstance(result, EmailData)
    assert result.subject == ""
    assert result.recipients == []
    assert result.sender is None
    assert result.message_id is None


def test_parse_email_rejects_bad_account_id():
    with pytest.raises(ValueError, match="account_id"):
        parse_email(RawEmail(account_id=0, uid=1, raw=_VALID_RAW))


def test_parse_email_rejects_bad_uid():
    with pytest.raises(ValueError, match="uid"):
        parse_email(RawEmail(account_id=1, uid=-1, raw=_VALID_RAW))


def test_parse_email_malformed_raw_is_tolerated():
    result = parse_email(RawEmail(account_id=1, uid=1, raw=b"not a valid email at all"))

    assert isinstance(result, EmailData)
    assert result.uid == 1
    assert result.subject == ""
