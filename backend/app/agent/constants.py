"""analysis 图层可调常量：语言检测区间与 LLM 节点调用参数。

集中存放 analysis_graph.py 使用的魔法数字，调整节点行为参数只改这里。
"""

# ---------------------------------------------------------------------------
# 语言检测：Unicode 脚本区间（_is_chinese_dominant_text 使用）
# ---------------------------------------------------------------------------

#: 平假名 + 片假名（出现即判定日语，非中文）
KANA_RANGE: tuple[int, int] = (0x3040, 0x30FF)
#: 谚文音节（出现即判定韩语，非中文）
HANGUL_RANGE: tuple[int, int] = (0xAC00, 0xD7AF)
#: CJK 统一表意文字（汉字）
HAN_RANGE: tuple[int, int] = (0x4E00, 0x9FFF)

# ---------------------------------------------------------------------------
# LLM 节点调用参数（analyze / detect_and_translate 共用）
# ---------------------------------------------------------------------------

#: 单次 LLM 调用超时（秒），节点内 asyncio.wait_for 使用
LLM_CALL_TIMEOUT_SECONDS: int = 120
#: 送入 LLM 的正文截断上限（字符），与 preprocess 清洗截断一致
ANALYSIS_MAX_BODY_CHARS: int = 6000
#: 节点级 RetryPolicy 最大尝试次数（含首次）
LLM_NODE_MAX_ATTEMPTS: int = 2
