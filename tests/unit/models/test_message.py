from datetime import UTC, datetime

import pytest

from app.models.message import EmailMessage


def test_message_creation_with_required_fields_succeeds():
    msg = EmailMessage(account_id=1, uid=100)
    assert msg.account_id == 1
    assert msg.uid == 100


def test_message_defaults_align_with_db_schema():
    msg = EmailMessage(account_id=1, uid=1)
    assert msg.message_id is None
    assert msg.subject == ""
    assert msg.sender is None
    assert msg.recipients == []
    assert msg.sent_at is None
    assert msg.text_body is None
    assert msg.html_body is None
    assert msg.fetched_at is not None


def test_message_recipients_default_is_not_shared():
    a = EmailMessage(account_id=1, uid=1)
    b = EmailMessage(account_id=1, uid=2)
    a.recipients.append("x@example.com")
    assert b.recipients == []


def test_message_all_fields_round_trip():
    now = datetime(2026, 8, 25, tzinfo=UTC)
    msg = EmailMessage(
        id=5,
        account_id=2,
        uid=42,
        message_id="<abc@example.com>",
        subject="hello",
        sender="from@example.com",
        recipients=["to@example.com", "cc@example.com"],
        sent_at=now,
        text_body="plain",
        html_body="<p>hi</p>",
        fetched_at=now,
    )
    assert msg.subject == "hello"
    assert msg.recipients == ["to@example.com", "cc@example.com"]
    assert msg.text_body == "plain"


def test_message_uid_negative_raises():
    with pytest.raises(ValueError):
        EmailMessage(account_id=1, uid=-1)


def test_message_subject_none_coerced_to_empty():
    msg = EmailMessage(account_id=1, uid=1, subject=None)  # type: ignore[arg-type]
    assert msg.subject == ""
