from __future__ import annotations

from email.header import Header
from email.message import EmailMessage as StdEmailMessage
from email.policy import default

from email_agent.parsing.parser import parse_email


def _build_email(
    *,
    subject: str | Header | None = None,
    from_addr: str | None = None,
    to_addrs: list[str] | None = None,
    cc_addrs: list[str] | None = None,
    date: str | None = None,
    message_id: str | None = None,
    text_body: str | None = None,
    html_body: str | None = None,
    extra_parts: list[tuple[str, str, str]] | None = None,
) -> bytes:
    """Build a raw RFC822 message with ``email`` stdlib, return as_bytes."""
    msg = StdEmailMessage(policy=default)
    if subject is not None:
        if isinstance(subject, Header):
            msg["Subject"] = subject.encode()
        else:
            msg["Subject"] = subject  # Header or str
    if from_addr is not None:
        msg["From"] = from_addr
    if to_addrs:
        msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    if date is not None:
        msg["Date"] = date
    if message_id is not None:
        msg["Message-ID"] = message_id

    # body handling
    if text_body is not None and html_body is not None:
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
    elif text_body is not None:
        msg.set_content(text_body)
    elif html_body is not None:
        msg.set_content(html_body, subtype="html")

    if extra_parts:
        # extra_parts: list of (content, subtype, disposition)
        # to test attachments, etc. Convert to mixed if needed
        for content, subtype, disp in extra_parts:
            # content is already str/bytes
            payload = content.encode() if isinstance(content, str) else content
            if disp == "attachment":
                msg.add_attachment(
                    payload, maintype="application", subtype=subtype, filename="file.pdf"
                )
            else:
                msg.add_attachment(payload, maintype="text", subtype=subtype)

    return msg.as_bytes()


def test_parse_subject_gbk_encoded_decodes_correctly():
    # Header with gbk charset
    hdr = Header("你好", "gbk")
    raw = _build_email(subject=hdr, from_addr="a@example.com", text_body="hi")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.subject == "你好"


def test_parse_recipients_multiple_to_and_cc_merged_deduped():
    raw = _build_email(
        from_addr="s@example.com",
        to_addrs=["a@x.com", "b@x.com"],
        cc_addrs=["b@x.com", "c@x.com"],
        subject="test",
        text_body="hi",
    )
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.recipients == ["a@x.com", "b@x.com", "c@x.com"]


def test_parse_subject_missing_returns_empty():
    raw = _build_email(from_addr="a@example.com", text_body="hi")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.subject == ""


def test_parse_body_html_only_html_body_filled():
    raw = _build_email(from_addr="a@example.com", subject="s", html_body="<p>hello</p>")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.html_body is not None and "<p>hello</p>" in parsed.html_body
    assert parsed.text_body is None


def test_parse_body_text_only_text_body_filled():
    raw = _build_email(from_addr="a@example.com", subject="s", text_body="plain hello")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.text_body is not None and "plain hello" in parsed.text_body
    assert parsed.html_body is None


def test_parse_body_both_plain_and_html_both_filled():
    raw = _build_email(
        from_addr="a@example.com", subject="s", text_body="plain", html_body="<p>html</p>"
    )
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.text_body is not None and "plain" in parsed.text_body
    assert parsed.html_body is not None and "html" in parsed.html_body


def test_parse_sent_at_invalid_returns_none():
    raw = _build_email(from_addr="a@example.com", subject="s", text_body="hi", date="not-a-date")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.sent_at is None


def test_parse_sent_at_valid_parsed():
    raw = _build_email(
        from_addr="a@example.com",
        subject="s",
        text_body="hi",
        date="Wed, 21 Aug 2026 10:00:00 +0800",
    )
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.sent_at is not None


def test_parse_empty_raw_returns_defaults():
    parsed = parse_email(b"", account_id=1, uid=99)
    assert parsed.account_id == 1
    assert parsed.uid == 99
    assert parsed.subject == ""
    assert parsed.sender is None
    assert parsed.recipients == []
    assert parsed.sent_at is None


def test_parse_sender_extracts_addr_spec():
    raw = _build_email(from_addr="张三 <zhang@example.com>", subject="s", text_body="hi")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.sender == "zhang@example.com"


def test_parse_sender_simple_addr():
    raw = _build_email(from_addr="simple@example.com", subject="s", text_body="hi")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.sender == "simple@example.com"


def test_parse_message_id_preserved():
    raw = _build_email(
        from_addr="a@example.com", subject="s", text_body="hi", message_id="<abc@x.com>"
    )
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.message_id == "<abc@x.com>"


def test_parse_message_id_missing_is_none():
    raw = _build_email(from_addr="a@example.com", subject="s", text_body="hi")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.message_id is None


def test_parse_attachment_skipped():
    # build mixed with attachment
    msg = StdEmailMessage(policy=default)
    msg["From"] = "a@example.com"
    msg["Subject"] = "with attachment"
    msg["To"] = "b@example.com"
    msg.set_content("body text")
    msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="file.pdf")
    raw = msg.as_bytes()
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.text_body is not None and "body text" in parsed.text_body
    # attachment content must not leak into body
    assert "%PDF" not in (parsed.text_body or "")
    assert "%PDF" not in (parsed.html_body or "")


def test_parse_recipients_empty_returns_empty_list():
    raw = _build_email(from_addr="a@example.com", subject="s", text_body="hi")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.recipients == []


def test_parse_subject_none_coerced():
    # Explicitly no subject header already tested; also check with raw that has empty subject
    raw = _build_email(from_addr="a@example.com", subject="", text_body="hi")
    parsed = parse_email(raw, account_id=1, uid=1)
    assert parsed.subject == ""
