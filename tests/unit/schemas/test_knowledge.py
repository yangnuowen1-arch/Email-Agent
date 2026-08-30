"""app.schemas.knowledge 单元测试：知识库常量白名单完整性。

常量是 db 层白名单校验与 docs/db-schema.md §2.8 的单一来源，
本测试守住"代码常量与文档枚举一致"这条约定。
"""

from __future__ import annotations

from typing import get_args

from app.schemas.knowledge import (
    ALL_KB_SOURCE_TYPES,
    ALL_KB_STATUSES,
    ALL_KB_TYPES,
    KB_EMBEDDING_DIMENSIONS,
    KB_SOURCE_TYPE_FILE,
    KB_SOURCE_TYPE_LITERAL,
    KB_SOURCE_TYPE_MAIL,
    KB_SOURCE_TYPE_TEXT,
    KB_STATUS_ACTIVE,
    KB_STATUS_ARCHIVED,
    KB_STATUS_LITERAL,
    KB_TYPE_COMPLIANCE,
    KB_TYPE_FAQ,
    KB_TYPE_LITERAL,
    KB_TYPE_SOP,
)


class TestKbTypes:
    def test_type_constants_match_whitelist(self):
        assert {
            KB_TYPE_FAQ: ALL_KB_TYPES[KB_TYPE_FAQ],
            KB_TYPE_SOP: ALL_KB_TYPES[KB_TYPE_SOP],
            KB_TYPE_COMPLIANCE: ALL_KB_TYPES[KB_TYPE_COMPLIANCE],
        } == ALL_KB_TYPES
        assert set(ALL_KB_TYPES) == {"faq", "sop", "compliance"}

    def test_type_literal_covers_whitelist(self):
        assert set(get_args(KB_TYPE_LITERAL)) == set(ALL_KB_TYPES)

    def test_each_type_has_chinese_meaning(self):
        assert all(isinstance(v, str) and v for v in ALL_KB_TYPES.values())


class TestKbSourceTypes:
    def test_source_type_constants_match_whitelist(self):
        assert set(ALL_KB_SOURCE_TYPES) == {
            KB_SOURCE_TYPE_MAIL,
            KB_SOURCE_TYPE_FILE,
            KB_SOURCE_TYPE_TEXT,
        }
        assert set(ALL_KB_SOURCE_TYPES) == {"mail", "file", "text"}

    def test_source_type_literal_covers_whitelist(self):
        assert set(get_args(KB_SOURCE_TYPE_LITERAL)) == set(ALL_KB_SOURCE_TYPES)


class TestKbStatuses:
    def test_status_constants_match_whitelist(self):
        assert set(ALL_KB_STATUSES) == {KB_STATUS_ACTIVE, KB_STATUS_ARCHIVED}
        assert set(ALL_KB_STATUSES) == {"active", "archived"}

    def test_status_literal_covers_whitelist(self):
        assert set(get_args(KB_STATUS_LITERAL)) == set(ALL_KB_STATUSES)


class TestEmbeddingDimensions:
    def test_dimensions_anchor(self):
        # 与 docs/db-schema.md §2.7 的 vector(1536) 严格对齐；改这里必须先走文档变更
        assert KB_EMBEDDING_DIMENSIONS == 1536
