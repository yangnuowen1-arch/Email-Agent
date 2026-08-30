"""rag.chunking 单元测试：分段装填、超长硬切与参数校验。"""

from __future__ import annotations

import pytest

from app.rag.chunking import chunk_text


class TestChunkTextBasics:
    def test_empty_and_whitespace_return_empty(self) -> None:
        assert chunk_text("") == []
        assert chunk_text("   \n \n\t ") == []

    def test_non_string_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="text must be str"):
            chunk_text(None)  # type: ignore[arg-type]

    def test_single_short_paragraph_single_chunk(self) -> None:
        assert chunk_text("只有一个短段落") == ["只有一个短段落"]

    def test_paragraphs_packed_into_one_chunk_preserving_separators(self) -> None:
        text = "第一段\n\n第二段\n\n第三段"
        chunks = chunk_text(text)
        assert chunks == ["第一段\n\n第二段\n\n第三段"]

    def test_deterministic_for_same_input(self) -> None:
        text = "FAQ 问题一\n\n答案一\n\nFAQ 问题二\n\n答案二"
        assert chunk_text(text) == chunk_text(text)


class TestChunkTextPacking:
    def test_packing_respects_max_chars(self) -> None:
        # 每段 6 字，max_chars=10：6 + 2(分隔符) + 6 > 10，必须拆成两块
        text = "一二三四五六\n\n一二三四五六"
        chunks = chunk_text(text, max_chars=10, overlap_chars=0)
        assert chunks == ["一二三四五六", "一二三四五六"]

    def test_packing_fills_blocks_to_limit(self) -> None:
        # 每段 4 字，max_chars=10：4+2+4=10 恰好装进一块
        text = "一二三四\n\n一二三四"
        chunks = chunk_text(text, max_chars=10, overlap_chars=0)
        assert chunks == ["一二三四\n\n一二三四"]

    def test_long_paragraph_hard_split_with_overlap(self) -> None:
        paragraph = "甲" * 25
        chunks = chunk_text(paragraph, max_chars=10, overlap_chars=5)
        # step = 10 - 5 = 5 → 窗口起点 0,5,10,15,20
        assert len(chunks) == 5
        assert chunks[0] == "甲" * 10
        assert chunks[1] == "甲" * 10
        # 相邻块重叠 5 个字符：chunk[i][5:] == chunk[i+1][:5]
        assert chunks[0][5:] == chunks[1][:5]

    def test_hard_split_last_window_shorter(self) -> None:
        chunks = chunk_text("甲" * 23, max_chars=10, overlap_chars=5)
        assert len(chunks) == 5
        assert chunks[-1] == "甲" * 3

    def test_hard_split_without_overlap(self) -> None:
        chunks = chunk_text("甲" * 20, max_chars=10, overlap_chars=0)
        assert chunks == ["甲" * 10, "甲" * 10]

    def test_mixed_long_and_short_paragraphs(self) -> None:
        text = "甲" * 25 + "\n\n" + "短段"
        chunks = chunk_text(text, max_chars=10, overlap_chars=5)
        # 25 字硬切成 5 个窗口（10,10,10,10,5），尾部 5 字窗口与"短段"装得下同一块
        assert len(chunks) == 5
        assert chunks[:4] == ["甲" * 10] * 4
        assert chunks[-1] == "甲" * 5 + "\n\n短段"


class TestChunkTextValidation:
    @pytest.mark.parametrize("max_chars", [0, -1])
    def test_max_chars_must_be_positive(self, max_chars: int) -> None:
        with pytest.raises(ValueError, match="max_chars"):
            chunk_text("内容", max_chars=max_chars)

    def test_overlap_must_be_less_than_max(self) -> None:
        with pytest.raises(ValueError, match="overlap_chars"):
            chunk_text("内容", max_chars=10, overlap_chars=10)

    def test_overlap_must_be_non_negative(self) -> None:
        with pytest.raises(ValueError, match="overlap_chars"):
            chunk_text("内容", max_chars=10, overlap_chars=-1)
