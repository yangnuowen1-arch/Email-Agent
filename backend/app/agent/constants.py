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

# ---------------------------------------------------------------------------
# 附件参与分析（_analyze_node 内附件内容提取）
# ---------------------------------------------------------------------------

#: 壳层正文低于该长度视为"极短"，才对图片附件做视觉识别（成本控制）
SHORT_SHELL_CHARS: int = 200
#: 单封邮件最多识别的图片附件数
MAX_IMAGES_PER_EMAIL: int = 3

# ---------------------------------------------------------------------------
# 回复草稿（draft_presale / draft_aftersale 节点，RAG 检索门槛）
# ---------------------------------------------------------------------------

#: 草稿节点检索的知识块条数
DRAFT_RETRIEVAL_TOP_K: int = 4
#: 检索质量门槛：最近一条的余弦距离超过该值视为无相关知识，不出草稿。
#: 该值与 embedding 模型强相关，上线后按真实召回分布调整
DRAFT_MAX_COSINE_DISTANCE: float = 0.8
#: 检索 query 截断长度（主题 + 正文拼接后截断）
DRAFT_QUERY_MAX_CHARS: int = 500
#: state 检索证据（retrieved_chunks）的 content 截断长度；与落库 draft_sources 的
#: snippet（200 字）分开：证据供日志排查"为什么没出草稿"，保留更多上下文
DRAFT_CHUNK_SNIPPET_CHARS: int = 500
#: 草稿 prompt 注入红线规则的总字符预算（整条规则为单位，放不下整条就舍弃其后全部）
DRAFT_COMPLIANCE_MAX_CHARS: int = 2000
