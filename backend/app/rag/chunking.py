"""切块：把整篇知识原文切成喂给 embedding 模型的片段。

纯函数、无 I/O、确定可复现——同一篇文本永远得到同一组分块，
这是 content_hash 之外保证重入库结果稳定的前提。

策略取中文业务文档的常见形态（FAQ 问答、SOP 条目、红线规则多为
空行/换行分隔的短段落）：空行分段为原子单元，贪心装填到 max_chars；
单段超长时按滑动窗口硬切并保留 overlap，避免语义被截断在窗口边缘。
"""

from __future__ import annotations

import re

# 空行分段：两个及以上换行（可夹空白）视为段落边界
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def chunk_text(text: str, *, max_chars: int = 500, overlap_chars: int = 50) -> list[str]:
    """按段落贪心装填切块，返回非空片段列表（保持原文顺序）。

    - ``max_chars``：单块字符数上限（中文一字一符，直接用字符数衡量）
    - ``overlap_chars``：单段超长硬切时相邻块的重叠字符数，衔接上下文
    - 空白文本返回 ``[]``；调用方（ingest）负责在空结果时报错
    """
    if not isinstance(max_chars, int) or max_chars <= 0:
        msg = f"max_chars must be positive int, got {max_chars!r}"
        raise ValueError(msg)
    if not isinstance(overlap_chars, int) or not 0 <= overlap_chars < max_chars:
        msg = f"overlap_chars must be int in [0, {max_chars}), got {overlap_chars!r}"
        raise ValueError(msg)
    if not isinstance(text, str):
        msg = f"text must be str, got {type(text).__name__}"
        raise TypeError(msg)
    if not text.strip():
        return []

    units: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT.split(text.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            units.append(paragraph)
        else:
            units.extend(_split_long(paragraph, max_chars=max_chars, overlap_chars=overlap_chars))

    # 贪心装填：相邻单元能塞进同一块就不切，块间用双换行还原段落感
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks


def _split_long(paragraph: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    """超长段落按滑动窗口硬切：窗口 max_chars，步进 max_chars - overlap_chars。"""
    step = max_chars - overlap_chars
    return [paragraph[start : start + max_chars] for start in range(0, len(paragraph), step)]
