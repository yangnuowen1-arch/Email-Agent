"""生成 scripts/kb_seed.sql：RAG 知识库种子数据（确定性，可重复生成）。

用法：
    .venv/bin/python scripts/gen_kb_seed.py

设计说明：
- 向量为**占位向量**（M2 接真实 embedding 前用于结构验证）：按 kb_type 划分
  聚簇中心 + 确定性微噪声，使同类型块的余弦距离明显小于跨类型，
  手工跑相似度 SQL 能看到符合直觉的排序；embedding_model 标记为
  'seed-dummy-1536'，M2 用真实模型名检索时天然不会命中种子数据。
- random.Random(种子) 逐块独立播种 → 同一版本代码重跑产出逐字节相同的 SQL。
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

DIM = 1536
MODEL = "seed-dummy-1536"
NOISE = 0.02  # 块向量在聚簇中心附近的扰动幅度

# ---------------------------------------------------------------------------
# 文档与切片内容（覆盖三类知识：faq / sop / compliance）
# ---------------------------------------------------------------------------

DOCUMENTS: list[dict] = [
    {
        "source_key": "file:docs/faq-pricing-v1.md",
        "kb_type": "faq",
        "title": "产品与价格 FAQ",
        "source_type": "file",
        "status": "active",
    },
    {
        "source_key": "file:docs/faq-pricing-2025.md",
        "kb_type": "faq",
        "title": "旧版价格 FAQ（已归档）",
        "source_type": "file",
        "status": "archived",
    },
    {
        "source_key": "text:sop-email-etiquette-v1",
        "kb_type": "sop",
        "title": "商务邮件沟通 SOP",
        "source_type": "text",
        "status": "active",
    },
    {
        "source_key": "text:compliance-redline-v1",
        "kb_type": "compliance",
        "title": "回复红线规则",
        "source_type": "text",
        "status": "active",
    },
]

# (source_key, chunk_index, 切片原文, metadata)
CHUNKS: list[tuple[str, int, str, dict]] = [
    (
        "file:docs/faq-pricing-v1.md",
        0,
        "SmartLink 智能邮件助手是一款面向中小企业的邮件自动处理工具：自动拉取多账号邮箱邮件、"
        "识别客户意图、生成结构化分析与回复草稿。支持中文、英文、日文等语言的自动检测与翻译。",
        {"tags": ["product_intro"], "audience": "all"},
    ),
    (
        "file:docs/faq-pricing-v1.md",
        1,
        "当前报价：标准版 299 元/月（含 3 个邮箱账号），专业版 899 元/月"
        "（含 10 个邮箱账号与附件解析），企业版按需报价。年度付费享两个月减免。"
        "折扣政策需经销售总监审批。",
        {"tags": ["pricing"], "audience": "tob"},
    ),
    (
        "file:docs/faq-pricing-v1.md",
        2,
        "标准交付周期：SaaS 版开通后 1 个工作日内交付；私有化部署含环境搭建与数据迁移，"
        "通常 5 至 10 个工作日。加急交付需额外排期确认。",
        {"tags": ["delivery"], "audience": "tob"},
    ),
    (
        "file:docs/faq-pricing-2025.md",
        0,
        "（已归档，2025 年旧价）标准版 199 元/月，专业版 699 元/月。该价格已失效，"
        "仅作历史记录保留，回复客户时一律使用最新报价。",
        {"tags": ["pricing"], "audience": "tob", "deprecated": True},
    ),
    (
        "text:sop-email-etiquette-v1",
        0,
        "称呼规范：首次联系客户用「尊敬的 X 先生/女士」；已有往来的客户可用「X 总/X 经理」；"
        "内部同事直接用英文名。避免使用「亲爱的」「你好呀」等过于随意的称呼。",
        {"tags": ["salutation"]},
    ),
    (
        "text:sop-email-etiquette-v1",
        1,
        "落款规范：对外邮件统一使用公司签名档（姓名/职位/公司/电话），以「祝商祺」或"
        "「顺祝工作顺利」收尾；回复投诉类邮件以「感谢您的反馈与耐心」开头，先致意再解释。",
        {"tags": ["signature", "tone"]},
    ),
    (
        "text:sop-email-etiquette-v1",
        2,
        "报价回复模板：确认需求要点 → 给出对应版本与价格 → 说明有效期（30 天）→ "
        "主动邀约演示：「如需进一步了解，我们可以安排一次 30 分钟的线上演示，"
        "请问您本周哪天方便？」",
        {"tags": ["quote_reply", "template"]},
    ),
    (
        "text:compliance-redline-v1",
        0,
        "不可承诺事项：未经销售总监书面批准，不得向客户承诺任何折扣、赠送或延长账期；"
        "未经法务审阅，不得在邮件中对合同条款（违约金、知识产权、数据归属）作出解释或让步承诺。",
        {"tags": ["redline"]},
    ),
    (
        "text:compliance-redline-v1",
        1,
        "转人工触发词：客户来信出现「律师」「起诉」「监管投诉」「12315」「媒体采访」「数据泄露」"
        "等字样时，回复草稿仅生成事实确认与安抚话术，必须在草稿首行标注 [需人工复核] "
        "并停止自动发送。",
        {"tags": ["handoff"]},
    ),
]


def _cluster_center(kb_type: str) -> list[float]:
    """按 kb_type 确定性生成聚簇中心：同类型块彼此相近、跨类型明显远离。"""
    rng = random.Random(f"kb-seed-center:{kb_type}")
    return [rng.uniform(-1.0, 1.0) for _ in range(DIM)]


def _embedding(source_key: str, chunk_index: int, kb_type: str) -> list[float]:
    """聚簇中心 + 确定性微噪声；越界裁剪到 [-1, 1]。"""
    center = _cluster_center(kb_type)
    rng = random.Random(f"kb-seed-chunk:{source_key}:{chunk_index}")
    return [max(-1.0, min(1.0, c + rng.uniform(-NOISE, NOISE))) for c in center]


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _content_hash(source_key: str) -> str:
    """文档 content_hash：对全文（各切片按序拼接）取真实 SHA-256。"""
    joined = "\n".join(c for k, _, c, _ in CHUNKS if k == source_key)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def build_sql() -> str:
    lines: list[str] = [
        "-- RAG 知识库种子数据（由 scripts/gen_kb_seed.py 确定性生成，勿手改）",
        '-- 用法：psql "$DATABASE_URL" -f scripts/kb_seed.sql',
        "-- 注意：重复执行会重复插入；重跑前先取消下一行注释清空两表——",
        "-- TRUNCATE kb_chunks, kb_documents RESTART IDENTITY;",
        "-- 向量为占位数据（embedding_model='seed-dummy-1536'），仅用于结构与检索链路验证；",
        "-- M2 接入真实 embedding 后，按真实模型名检索不会命中这些行。",
        "",
        "BEGIN;",
        "",
        "-- 1. 文档（覆盖 faq / sop / compliance 三类，含一篇 archived 验证状态过滤）",
    ]

    for doc in DOCUMENTS:
        lines.append(
            "INSERT INTO kb_documents (kb_type, title, source_type, source_key, "
            "content_hash, status) VALUES ("
            f"{_sql_str(doc['kb_type'])}, {_sql_str(doc['title'])}, "
            f"{_sql_str(doc['source_type'])}, {_sql_str(doc['source_key'])}, "
            f"{_sql_str(_content_hash(doc['source_key']))}, {_sql_str(doc['status'])});"
        )

    lines.extend(["", "-- 2. 分块（embedding 为按 kb_type 聚簇的 1536 维占位向量）"])

    for source_key, chunk_index, content, meta in CHUNKS:
        doc = next(d for d in DOCUMENTS if d["source_key"] == source_key)
        vec = _vec_literal(_embedding(source_key, chunk_index, doc["kb_type"]))
        lines.append(
            "INSERT INTO kb_chunks (document_id, kb_type, chunk_index, content, "
            "embedding, embedding_model, metadata) VALUES ("
            f"(SELECT id FROM kb_documents WHERE source_key = {_sql_str(source_key)}), "
            f"{_sql_str(doc['kb_type'])}, {chunk_index}, {_sql_str(content)}, "
            f"{_sql_str(vec)}::vector, {_sql_str(MODEL)}, "
            f"{_sql_str(json.dumps(meta, ensure_ascii=False))}::jsonb);"
        )

    lines.extend(
        [
            "",
            "COMMIT;",
            "",
            "-- 示例：同库余弦检索（把查询向量换成真实 embedding 后使用）",
            "-- SELECT c.id, c.kb_type, c.chunk_index, left(c.content, 30) AS content_preview,",
            "--        c.embedding <=> (SELECT embedding FROM kb_chunks",
            "--            WHERE chunk_index = 0 LIMIT 1) AS distance",
            "-- FROM kb_chunks c",
            "-- WHERE c.kb_type = 'faq'",
            "--   AND c.embedding_model = 'seed-dummy-1536'",
            "--   AND c.document_id IN (SELECT id FROM kb_documents WHERE status = 'active')",
            "-- ORDER BY distance",
            "-- LIMIT 5;",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    out = Path(__file__).resolve().parent / "kb_seed.sql"
    out.write_text(build_sql(), encoding="utf-8")
    print(f"written: {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
