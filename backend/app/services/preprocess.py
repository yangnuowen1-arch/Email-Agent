"""邮件文本清洗：html→text、剥离签名/退订/免责声明、截断超长正文。

纯函数，不触及数据库或 LLM。供 analyze_email 工具调用。
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from bs4 import BeautifulSoup

# 归一化：连续 3+ 空行压成 1 个
_MULTI_BLANK = re.compile(r"\n{3,}")

# 退订/免责声明关键词
_UNSUBSCRIBE_PAT = re.compile(r"(?i)unsubscribe|退订|取消订阅|opt[-\s]?out")
_DISCLAIMER_PAT = re.compile(r"(?i)confidential|disclaimer|保密|机密|请勿回复|do not reply")

# 签名/引用截断标记
_SIGNATURE_MARKERS = re.compile(
    r"^.{0,80}(?:wrote:|写道：|写于)"  # 邮件客户端引用
    r"|^> "  # 邮件引用行
    r"|^-{3,}$"  # Outlook 分隔线
    r"|^_{3,}$",  # Outlook 分隔线
    re.MULTILINE,
)

# HTML 噪声标签
_NOISE_TAGS = {"style", "script", "head", "noscript", "template", "iframe", "svg"}


def html_to_text(raw_html: str) -> str:
    """将 HTML 正文转为纯文本，剥离噪声标签和隐藏元素。"""
    soup = BeautifulSoup(raw_html, "html.parser")

    # 剥离噪声标签
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

    # 剥离 hidden 属性元素
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()

    # 剥离 display:none / visibility:hidden 的 style
    for tag in soup.find_all(style=True):
        style = tag.get("style", "")
        if "display:none" in style.replace(" ", "") or "visibility:hidden" in style.replace(
            " ", ""
        ):
            tag.decompose()

    text = soup.get_text(separator="\n")
    return html.unescape(text)


def strip_boilerplate(text: str) -> str:
    """剥离签名、退订链接、免责声明等冗余内容。

    从截断点丢弃后续（而非逐行删），防止误删正文。
    """
    lines = text.split("\n")

    # 扫描签名/引用截断点
    truncate_at: int | None = None
    for i, line in enumerate(lines):
        if _SIGNATURE_MARKERS.search(line):
            truncate_at = i
            break

    if truncate_at is not None:
        lines = lines[:truncate_at]

    # 扫描尾部退订/免责声明块：从末尾往前删，遇正文停
    end = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if _UNSUBSCRIBE_PAT.search(line) or _DISCLAIMER_PAT.search(line):
            end = i
        elif line:
            # 遇到非空正文行，停止
            break

    lines = lines[:end]

    # 归一化
    text = "\n".join(lines)
    text = text.replace("\r\n", "\n")
    text = _MULTI_BLANK.sub("\n\n", text)
    # 行尾去空白
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def preprocess_email_text(
    text_body: str | None,
    html_body: str | None,
    *,
    max_chars: int = 6000,
) -> str:
    """清洗邮件正文：text 优先，空则 html→text，皆空返回空串。"""
    if text_body and text_body.strip():
        cleaned = strip_boilerplate(text_body)
    elif html_body and html_body.strip():
        cleaned = strip_boilerplate(html_to_text(html_body))
    else:
        return ""

    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "…(truncated)"

    return cleaned


def compose_email_view(
    *,
    subject: str,
    sender: str | None,
    sent_at: datetime | None,
    cleaned_text: str,
    attachment_views: list[dict] | None = None,
) -> str:
    """将邮件元数据、正文与附件提取内容拼成 LLM 消费的分层视图。

    ``attachment_views`` 每项: {"kind": "email"|"image", "filename": str, "text": str}；
    分别输出"转发邮件"/"图片内容"分层段并标注来源。不传参数时输出与
    旧版（仅元数据 + 正文）完全一致，保证向后兼容。
    """
    sent_at_str = sent_at.isoformat() if sent_at else "未知"
    parts = [
        f"发件人: {sender or '未知'}",
        f"主题: {subject or '(无主题)'}",
        f"时间: {sent_at_str}",
        "--- 正文 ---",
        cleaned_text if cleaned_text else "(正文为空)",
    ]
    for view in attachment_views or []:
        if view.get("kind") == "email":
            label = f"--- 转发邮件（附件：{view.get('filename') or '未命名.eml'}）---"
        else:
            label = f"--- 图片内容（附件：{view.get('filename') or '未命名图片'}，视觉识别）---"
        text = (view.get("text") or "").strip()
        parts.append(label)
        parts.append(text if text else "(未能识别)")
    return "\n".join(parts)
