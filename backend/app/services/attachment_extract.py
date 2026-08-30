"""附件内容提取：.eml 递归解析（纯函数）与图片视觉识别（模型显式注入）。

供 ``analysis_graph._analyze_node`` 组装分层视图前调用；本模块不触 DB，
提取结果由 coordinator 写回 ``email_attachments.extracted_text`` 缓存，
重复分析不重复调用视觉模型。
"""

from __future__ import annotations

import base64

from langchain_core.messages import HumanMessage

from app.schemas import RawEmail
from app.services.parsing import parse_email
from app.services.preprocess import preprocess_email_text

#: .eml 递归下钻深度上限（附件套附件不再展开）
MAX_EML_DEPTH: int = 2
#: 单段提取文本上限，防止长附件撑爆分析视图
MAX_EXTRACT_CHARS: int = 2000


def extract_eml_text(content: bytes, *, depth: int = 0) -> str | None:
    """解析 .eml / message/rfc822 字节，产出"发件人/主题/正文"文本段。

    嵌套邮件自带的 .eml 附件继续下钻（受 depth 限制）；
    无有效内容（主题正文全空）返回 None。
    """
    if depth >= MAX_EML_DEPTH or not content:
        return None
    # 借用合法占位键复用 parse_email（其校验要求 account_id>0），产物只用解析字段
    data = parse_email(RawEmail(account_id=1, uid=0, raw=content))
    cleaned = preprocess_email_text(data.text_body, data.html_body, max_chars=MAX_EXTRACT_CHARS)

    lines = [f"发件人: {data.sender or '未知'}", f"主题: {data.subject or '(无主题)'}"]
    if cleaned:
        lines.append(cleaned)
    for attachment in data.attachments:
        if attachment.kind == "email" and attachment.content:
            nested = extract_eml_text(attachment.content, depth=depth + 1)
            if nested:
                lines.append("--- 附件中的邮件 ---")
                lines.append(nested)
    return "\n".join(lines) if len(lines) > 2 else None


async def extract_image_text(content: bytes, mime_type: str, vision_model) -> str | None:
    """调用视觉 LLM 提取图片文字并概括图片类型；未配置或调用失败返回 None。

    失败在此处降级而非抛错：调用方（analyze 节点）不因单张图片识别失败
    中断整封邮件的分析。
    """
    if vision_model is None or not content:
        return None
    b64 = base64.b64encode(content).decode("ascii")
    message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": (
                    "提取图片中的全部文字，并用一句话说明图片类型"
                    "（发票/订单截图/报错截图/营销海报/照片等）。"
                ),
            },
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        ]
    )
    try:
        result = await vision_model.ainvoke([message])
    except Exception:  # noqa: BLE001
        return None
    return result.content if isinstance(result.content, str) else None
