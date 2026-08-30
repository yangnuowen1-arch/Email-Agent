from __future__ import annotations

import email
from email import policy
from email.header import decode_header
from email.message import EmailMessage as StdEmailMessage
from email.utils import getaddresses, parsedate_to_datetime

from app.schemas import EmailData, ParsedAttachment, RawEmail

# 单附件字节上限：超过只保留元数据，不携带内容（不上传、不参与提取）
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _decode_subject(msg: StdEmailMessage) -> str:
    """解码邮件主题，处理各种编码的 encoded-word，失败时回退为空串。"""
    raw = msg["Subject"]
    # 无主题头时直接返回空串，符合模型默认值
    if raw is None:
        return ""

    # policy=default 下 raw 已部分解码，但仍需处理 encoded-words
    # 为保证健壮性，对缺失字符集的情况使用 utf-8 + replace 策略
    try:
        fragments = decode_header(str(raw))
    except Exception:
        # 解码过程异常时，尽力返回原始字符串，避免抛异常中断整批处理
        return str(raw) if isinstance(raw, str) else ""

    parts: list[str] = []
    for data, charset in fragments:
        if isinstance(data, str):
            # 已是解码后的字符串，直接拼接
            parts.append(data)
        else:
            # data 为字节，需按声明的 charset 解码
            cs = charset or "utf-8"
            try:
                parts.append(data.decode(cs, errors="replace"))
            except LookupError:
                # 未知字符集时回退到 utf-8
                parts.append(data.decode("utf-8", errors="replace"))
            except Exception:
                # 其他解码异常也回退到 utf-8，避免主题解析失败
                parts.append(data.decode("utf-8", errors="replace"))
    text = "".join(parts)
    return text


def _extract_sender(msg: StdEmailMessage) -> str | None:
    """提取发件人地址，优先使用结构化地址，失败时回退到原始字符串。"""
    hdr = msg["From"]
    if hdr is None:
        return None
    # policy=default 提供的 AddressHeader 包含结构化地址列表
    try:
        addrs = hdr.addresses  # type: ignore[attr-defined]
        if addrs:
            spec = addrs[0].addr_spec
            if spec:
                return spec
    except Exception:
        # 结构化解析失败，继续尝试回退方案
        pass
    # 回退：使用 getaddresses 解析逗号分隔的地址列表
    try:
        addrs2 = getaddresses([str(hdr)])
        for _name, addr in addrs2:
            if addr:
                return addr
    except Exception:
        return None
    # 最后兜底：返回原始头字符串的去空白结果
    raw = str(hdr).strip()
    return raw or None


def _extract_recipients(msg: StdEmailMessage) -> list[str]:
    """提取收件人列表（To + Cc 合并），去重且保持原序。"""
    collected: list[str] = []
    # 同时处理 To 和 Cc 头，合并为收件人全集
    for header_name in ("To", "Cc"):
        headers = msg.get_all(header_name, [])
        for hdr in headers:
            # 优先使用结构化的 addresses 属性
            try:
                addrs = hdr.addresses  # type: ignore[attr-defined]
                for a in addrs:
                    if a.addr_spec:
                        collected.append(a.addr_spec)
                continue
            except Exception:
                pass
            # 回退：getaddresses 可处理逗号分隔的多地址
            try:
                for _name, addr in getaddresses([str(hdr)]):
                    if addr:
                        collected.append(addr)
            except Exception:
                continue
    # 去重但保持首次出现顺序，避免重复入库
    seen: set[str] = set()
    deduped: list[str] = []
    for addr in collected:
        if addr not in seen:
            seen.add(addr)
            deduped.append(addr)
    return deduped


def _parse_sent_at(msg: StdEmailMessage):  # -> datetime | None
    """解析邮件发送时间，优先使用已解析的 datetime，失败则尝试字符串解析。"""
    hdr = msg["Date"]
    if hdr is None:
        return None
    # policy=default 场景下 Date 头可能已是 datetime 对象
    try:
        import datetime

        if isinstance(hdr, datetime.datetime):
            return hdr
    except Exception:
        pass
    # 回退：使用 parsedate_to_datetime 解析标准日期字符串
    try:
        dt = parsedate_to_datetime(str(hdr))
        return dt
    except Exception:
        # 解析失败返回 None，调用方可容错处理
        return None


def _extract_bodies(msg: StdEmailMessage) -> tuple[str | None, str | None]:
    """提取纯文本和 HTML 正文，处理多部分邮件和单部分邮件的各种情况。"""
    text_body: str | None = None
    html_body: str | None = None

    # 优先使用 policy=default 提供的 get_body 方法，按偏好提取对应类型
    try:
        if hasattr(msg, "get_body"):
            # 提取纯文本部分
            tpart = msg.get_body(preferencelist=("plain",))  # type: ignore[attr-defined]
            if tpart is not None:
                try:
                    c = tpart.get_content()
                    if isinstance(c, str):
                        text_body = c
                except Exception:
                    pass
            # 提取 HTML 部分
            hpart = msg.get_body(preferencelist=("html",))  # type: ignore[attr-defined]
            if hpart is not None:
                try:
                    c = hpart.get_content()
                    if isinstance(c, str):
                        html_body = c
                except Exception:
                    pass
    except Exception:
        pass

    # 若仍有缺失，通过 walk 遍历所有部分进行兜底提取，兼顾非 multipart 情况
    if text_body is None or html_body is None:
        try:
            if msg.is_multipart():
                # 多部分邮件：遍历所有子部分，跳过附件和嵌套 multipart 容器
                for part in msg.walk():
                    if part.is_multipart():
                        continue
                    if part.get_content_disposition() == "attachment":
                        # 附件不作为正文
                        continue
                    ctype = part.get_content_type()
                    try:
                        content = part.get_content()
                    except Exception:
                        continue
                    if not isinstance(content, str):
                        continue
                    # 按内容类型分别填充，首次匹配即保留，避免覆盖
                    if ctype == "text/plain" and text_body is None:
                        text_body = content
                    elif ctype == "text/html" and html_body is None:
                        html_body = content
                    # 两者都已找到则提前结束
                    if text_body is not None and html_body is not None:
                        break
            else:
                # 单部分邮件：直接取内容
                ctype = msg.get_content_type()
                disp = msg.get_content_disposition()
                if disp == "attachment":
                    # 单部分但标记为附件时，不作为正文
                    pass
                else:
                    try:
                        content = msg.get_content()
                    except Exception:
                        content = None
                    if isinstance(content, str):
                        if ctype == "text/plain" and text_body is None:
                            text_body = content
                        elif ctype == "text/html" and html_body is None:
                            html_body = content
                        elif (
                            text_body is None
                            and html_body is None
                            and content
                            and ctype.startswith("text/")
                        ):
                            # 未知的 text/* 类型，回退视为纯文本
                            text_body = content
                    # policy=default 下 get_content 返回 str，bytes 情况可忽略
        except Exception:
            pass

    return text_body, html_body


def _attachment_kind(part: StdEmailMessage) -> str:
    """按内容类型与文件名归类附件：email（.eml/message/rfc822）、image、document。"""
    ctype = part.get_content_type()
    filename = (part.get_filename() or "").lower()
    if ctype == "message/rfc822" or filename.endswith(".eml"):
        return "email"
    if ctype.startswith("image/"):
        return "image"
    return "document"


def _iter_attachment_parts(msg: StdEmailMessage):
    """深度优先遍历 MIME 叶子部分；message/rfc822 整体产出、不下钻。

    嵌套邮件（message/rfc822）由 :func:`parse_email` 递归解析单独处理，
    若下钻会把它内部的正文/图片错误地当成外层邮件的附件重复收集。
    """
    queue: list[StdEmailMessage] = [msg]
    while queue:
        part = queue.pop(0)
        if part.get_content_type() == "message/rfc822":
            yield part
            continue
        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                queue.extend(payload)
            continue
        yield part


def _extract_attachments(msg: StdEmailMessage) -> list[ParsedAttachment]:
    """收集全部附件（不再当噪声丢弃），按 kind 分类。

    图片无论 inline（内嵌 cid 图）还是 attachment 都收集；email 类取整段
    RFC822 字节供后续递归解析；其余类型仅收集 attachment 形态。
    收集失败不影响正文入库，单附件超上限只保留元数据。
    """
    attachments: list[ParsedAttachment] = []
    try:
        for part in _iter_attachment_parts(msg):
            ctype = part.get_content_type()
            disposition = part.get_content_disposition()
            filename = part.get_filename() or ""
            is_email = _attachment_kind(part) == "email"
            is_image = ctype.startswith("image/")
            if not is_email and not is_image and disposition != "attachment":
                continue
            try:
                if is_email:
                    # message/rfc822 的内容为嵌套 EmailMessage，还原为完整字节；
                    # 部分客户端以 octet-stream 传送 .eml，此时内容本身就是字节
                    inner = part.get_content()
                    if isinstance(inner, bytes):
                        payload: bytes | None = inner
                    elif hasattr(inner, "as_bytes"):
                        payload = inner.as_bytes(policy=policy.default)
                    else:
                        payload = None
                else:
                    payload = part.get_payload(decode=True)
            except Exception:
                payload = None
            content_id = part.get("Content-ID")
            over_limit = payload is None or len(payload) > MAX_ATTACHMENT_BYTES
            attachments.append(
                ParsedAttachment(
                    filename=filename,
                    content_type=ctype,
                    disposition=disposition,
                    content_id=str(content_id).strip(" <>") if content_id else None,
                    size=len(payload) if payload is not None else 0,
                    content=None if over_limit else payload,
                    kind=_attachment_kind(part),
                )
            )
    except Exception:
        # 附件收集异常时返回已收集部分，正文提取与入库不受影响
        pass
    return attachments


def parse_email(raw_email: RawEmail) -> EmailData:
    """将原始 RFC822 字节解析为领域数据 :class:`EmailData`。

    纯函数：无 I/O、无全局状态，便于单测和并发调用。
    """
    account_id = raw_email.account_id
    uid = raw_email.uid
    raw = raw_email.raw

    # 校验核心参数，防止非法值污染数据库
    if not isinstance(account_id, int) or account_id <= 0:
        msg = f"account_id must be positive int, got {account_id!r}"
        raise ValueError(msg)
    if not isinstance(uid, int) or uid < 0:
        msg = f"uid must be int >=0, got {uid!r}"
        raise ValueError(msg)

    # 空字节直接返回默认值，符合规范中“空邮件容错”的约定
    if not raw:
        return EmailData(account_id=account_id, uid=uid)

    try:
        # 使用 policy=default 解析，确保得到结构化的 EmailMessage 对象
        emsg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        # 解析完全失败时返回默认值，避免整批同步因单封邮件异常而中断
        return EmailData(account_id=account_id, uid=uid)

    # 依次提取各字段，每步均有容错逻辑
    subject = _decode_subject(emsg)
    sender = _extract_sender(emsg)
    recipients = _extract_recipients(emsg)
    sent_at = _parse_sent_at(emsg)

    # Message-ID 可能缺失，需去空白后归一为 None
    message_id = emsg["Message-ID"]
    if message_id is not None:
        message_id = str(message_id).strip() or None

    text_body, html_body = _extract_bodies(emsg)

    # 附件收集：不再丢弃，分类后随领域数据返回（字节仅内存传递）
    attachments = _extract_attachments(emsg)

    # 组装领域数据返回
    return EmailData(
        account_id=account_id,
        uid=uid,
        message_id=message_id,
        subject=subject,
        sender=sender,
        recipients=recipients,
        sent_at=sent_at,
        text_body=text_body,
        html_body=html_body,
        attachments=attachments,
    )
