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


def _multipart_raw_with_attachments() -> bytes:
    """构造一封带内嵌图、附件图、.eml 附件、pdf 附件的 multipart 邮件。"""
    boundary = "BOUNDARY42"
    eml_body = b"From: old@example.com\r\nSubject: old thread\r\n\r\nold body text\r\n"
    import base64

    png = base64.b64encode(b"\x89PNG-fake-image-bytes").decode()
    parts = [
        b"--" + boundary.encode(),
        b"Content-Type: text/plain; charset=utf-8",
        b"",
        b"see attachment",
        b"--" + boundary.encode(),
        b'Content-Type: image/png; name="inline.png"',
        b'Content-Disposition: inline; filename="inline.png"',
        b"Content-Transfer-Encoding: base64",
        b"Content-ID: <img1@host>",
        b"",
        png.encode(),
        b"--" + boundary.encode(),
        b'Content-Type: image/png; name="attached.png"',
        b'Content-Disposition: attachment; filename="attached.png"',
        b"Content-Transfer-Encoding: base64",
        b"",
        png.encode(),
        b"--" + boundary.encode(),
        b'Content-Type: message/rfc822; name="forwarded.eml"',
        b'Content-Disposition: attachment; filename="forwarded.eml"',
        b"",
        eml_body,
        b"--" + boundary.encode(),
        b'Content-Type: application/pdf; name="doc.pdf"',
        b'Content-Disposition: attachment; filename="doc.pdf"',
        b"",
        b"%PDF-fake",
        b"--" + boundary.encode() + b"--",
        b"",
    ]
    header = (
        b"From: Alice <alice@example.com>\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: with attachments\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="' + boundary.encode() + b'"\r\n'
        b"\r\n"
    )
    return header + b"\r\n".join(parts)


def test_parse_email_collects_and_classifies_attachments():
    result = parse_email(RawEmail(account_id=1, uid=10, raw=_multipart_raw_with_attachments()))

    kinds = [a.kind for a in result.attachments]
    assert kinds == ["image", "image", "email", "document"]
    inline, attached, eml_att, doc = result.attachments
    # 内嵌图：disposition=inline 且带去尖括号的 Content-ID
    assert inline.disposition == "inline"
    assert inline.content_id == "img1@host"
    assert inline.content == b"\x89PNG-fake-image-bytes"
    assert attached.content is not None
    # .eml 附件：还原为完整 RFC822 字节
    assert eml_att.kind == "email"
    assert eml_att.content is not None and b"old body text" in eml_att.content
    # document：只收集元数据
    assert doc.kind == "document" and doc.content == b"%PDF-fake"


def test_parse_email_plain_email_has_no_attachments():
    result = parse_email(RawEmail(account_id=1, uid=10, raw=_VALID_RAW))
    assert result.attachments == []


def test_parse_email_oversized_attachment_keeps_metadata_only(monkeypatch):
    import app.services.parsing as parsing

    monkeypatch.setattr(parsing, "MAX_ATTACHMENT_BYTES", 4)
    result = parse_email(RawEmail(account_id=1, uid=10, raw=_multipart_raw_with_attachments()))

    inline = result.attachments[0]
    assert inline.size == len(b"\x89PNG-fake-image-bytes")
    assert inline.content is None  # 超限只记元数据
