"""services.attachment_extract 单元测试：.eml 递归解析与图片视觉识别。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.attachment_extract import (
    MAX_EML_DEPTH,
    extract_eml_text,
    extract_image_text,
)
from app.services.preprocess import compose_email_view


def _eml_bytes(subject: str, body: str) -> bytes:
    return (
        b"From: Alice <alice@example.com>\r\n"
        b"Subject: " + subject.encode() + b"\r\n"
        b"\r\n" + body.encode() + b"\r\n"
    )


def test_extract_eml_text_formats_nested_email() -> None:
    text = extract_eml_text(_eml_bytes("Old thread", "old body text"))

    assert text is not None
    assert "alice@example.com" in text
    assert "Old thread" in text
    assert "old body text" in text


def test_extract_eml_text_empty_content_returns_none() -> None:
    assert extract_eml_text(b"") is None


def test_extract_eml_text_depth_limit_returns_none() -> None:
    assert extract_eml_text(_eml_bytes("s", "b"), depth=MAX_EML_DEPTH) is None


def test_extract_eml_text_recurses_into_nested_eml_attachment() -> None:
    inner = _eml_bytes("inner subject", "inner body")
    outer = (
        b"From: Bob <bob@example.com>\r\n"
        b"Subject: outer\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B1"\r\n'
        b"\r\n"
        b"--B1\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"outer body\r\n"
        b"--B1\r\n"
        b'Content-Type: message/rfc822; name="inner.eml"\r\n'
        b'Content-Disposition: attachment; filename="inner.eml"\r\n'
        b"\r\n" + inner + b"--B1--\r\n"
    )

    text = extract_eml_text(outer)

    assert text is not None
    assert "outer body" in text
    assert "--- 附件中的邮件 ---" in text
    assert "inner body" in text


class FakeVisionModel:
    """记录调用的假视觉模型；calls 为收到的消息列表，行为可注入。"""

    def __init__(self, *, reply: str = "发票照片，文字：ORD-123", error: Exception | None = None):
        self._reply = reply
        self._error = error
        self.calls: list[list] = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=self._reply)


async def test_extract_image_text_returns_model_reply() -> None:
    model = FakeVisionModel()

    text = await extract_image_text(b"png-bytes", "image/png", model)

    assert text == "发票照片，文字：ORD-123"
    assert len(model.calls) == 1
    # 消息包含 base64 图片 part
    assert any(part.get("type") == "image_url" for part in model.calls[0][0].content)


async def test_extract_image_text_without_model_returns_none() -> None:
    assert await extract_image_text(b"png-bytes", "image/png", None) is None


async def test_extract_image_text_empty_content_returns_none() -> None:
    assert await extract_image_text(b"", "image/png", FakeVisionModel()) is None


async def test_extract_image_text_model_error_degrades_to_none() -> None:
    model = FakeVisionModel(error=RuntimeError("vision down"))

    assert await extract_image_text(b"png-bytes", "image/png", model) is None


def test_compose_email_view_backward_compatible_without_attachments() -> None:
    """不传 attachment_views 时输出与旧版完全一致。"""
    sent_at = datetime(2026, 8, 30, tzinfo=UTC)
    view = compose_email_view(subject="s", sender="a@b.com", sent_at=sent_at, cleaned_text="hello")
    assert view == (
        "发件人: a@b.com\n主题: s\n时间: 2026-08-30T00:00:00+00:00\n--- 正文 ---\nhello"
    )


def test_compose_email_view_layered_sections() -> None:
    view = compose_email_view(
        subject="s",
        sender=None,
        sent_at=None,
        cleaned_text="see attachment",
        attachment_views=[
            {"kind": "email", "filename": "f.eml", "text": "发件人: a@b.com\n主题: old"},
            {"kind": "image", "filename": "p.png", "text": ""},
        ],
    )

    assert "--- 正文 ---" in view
    assert "--- 转发邮件（附件：f.eml）---" in view
    assert "发件人: a@b.com" in view
    assert "--- 图片内容（附件：p.png，视觉识别）---" in view
    assert "(未能识别)" in view
