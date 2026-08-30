"""EmailDraftRepository 单元测试：幂等 upsert（整体覆盖 + status 重置）与人工确认状态流转。

运行环境为内存 SQLite（email_drafts 的 sources 列经 with_variant 可建表）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.db import Base, EmailDraft
from app.db.repositories import EmailDraftRepository
from app.schemas.draft import DRAFT_STATUS_PENDING


@pytest.fixture
async def sqlite_engine():
    """内存 SQLite：仅创建 email_drafts 表（其余邮件表含 PG 方言列，与本题无关）。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[EmailDraft.__table__]))
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(sqlite_engine):
    maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


def _make_draft(**overrides) -> EmailDraft:
    fields = {
        "email_id": 1,
        "account_id": 1,
        "category": "presale",
        "subject": "Re: 产品价格咨询",
        "body": "您好，感谢来信。关于价格问题回复如下……",
        "sources": [{"document_id": 11, "distance": 0.2, "snippet": "价格政策……"}],
        "model": "gpt-4o-mini",
    }
    return EmailDraft(**{**fields, **overrides})


class TestUpsertEmailDraft:
    async def test_insert_returns_with_id_and_persists(self, session):
        repo = EmailDraftRepository(session)
        saved = await repo.upsert_email_draft(_make_draft())

        assert saved.id is not None
        found = await repo.get_email_draft_by_id(saved.id)
        assert found is not None
        assert found.email_id == 1
        assert found.category == "presale"
        assert found.status == DRAFT_STATUS_PENDING
        assert found.sources[0]["document_id"] == 11
        assert found.model == "gpt-4o-mini"

    async def test_upsert_overwrites_and_resets_status(self, session):
        """同 email_id 重生成：整体覆盖新内容，status 强制重置回 pending。"""
        repo = EmailDraftRepository(session)
        saved = await repo.upsert_email_draft(_make_draft())
        assert await repo.update_email_draft_status_by_id(saved.id, "approved") is True

        regenerated = await repo.upsert_email_draft(
            _make_draft(
                category="aftersale",
                subject="Re: 退款进度",
                body="您好，您的退款正在处理中。",
                sources=[{"document_id": 12, "distance": 0.4, "snippet": "退款政策……"}],
            )
        )

        assert regenerated.id == saved.id  # 唯一键命中同一行，不是新增
        found = await repo.get_email_draft_by_email_id(1)
        assert found is not None
        assert found.category == "aftersale"
        assert found.subject == "Re: 退款进度"
        assert found.body == "您好，您的退款正在处理中。"
        assert found.status == DRAFT_STATUS_PENDING
        assert found.sources[0]["document_id"] == 12

    async def test_upsert_rejects_non_entity(self, session):
        repo = EmailDraftRepository(session)
        with pytest.raises(TypeError, match="expected EmailDraft"):
            await repo.upsert_email_draft(MagicMock())


class TestDraftQueries:
    async def test_get_email_draft_by_id_not_found(self, session):
        repo = EmailDraftRepository(session)
        assert await repo.get_email_draft_by_id(99999) is None

    async def test_get_email_draft_by_email_id(self, session):
        repo = EmailDraftRepository(session)
        await repo.upsert_email_draft(_make_draft(email_id=7))

        found = await repo.get_email_draft_by_email_id(7)
        assert found is not None
        assert found.email_id == 7

    async def test_get_email_draft_by_email_id_not_found(self, session):
        repo = EmailDraftRepository(session)
        assert await repo.get_email_draft_by_email_id(99999) is None

    async def test_list_email_draft_by_status_orders_newest_first(self, session):
        repo = EmailDraftRepository(session)
        await repo.upsert_email_draft(_make_draft(email_id=1))
        await asyncio.sleep(0.002)  # 保证 created_at 时间戳可区分
        await repo.upsert_email_draft(_make_draft(email_id=2, subject="Re: 第二封"))

        drafts = await repo.list_email_draft_by_status(DRAFT_STATUS_PENDING)

        assert [d.email_id for d in drafts] == [2, 1]

    async def test_list_email_draft_by_status_filters(self, session):
        repo = EmailDraftRepository(session)
        saved = await repo.upsert_email_draft(_make_draft())
        await repo.update_email_draft_status_by_id(saved.id, "approved")
        await repo.upsert_email_draft(_make_draft(email_id=2))

        pending = await repo.list_email_draft_by_status(DRAFT_STATUS_PENDING)
        approved = await repo.list_email_draft_by_status("approved")

        assert [d.email_id for d in pending] == [2]
        assert [d.email_id for d in approved] == [1]


class TestUpdateEmailDraftStatus:
    async def test_update_status_persists(self, session):
        repo = EmailDraftRepository(session)
        saved = await repo.upsert_email_draft(_make_draft())

        assert await repo.update_email_draft_status_by_id(saved.id, "rejected") is True

        found = await repo.get_email_draft_by_id(saved.id)
        assert found is not None
        assert found.status == "rejected"

    async def test_update_status_not_found_returns_false(self, session):
        repo = EmailDraftRepository(session)
        assert await repo.update_email_draft_status_by_id(99999, "approved") is False

    async def test_update_status_rejects_non_whitelisted(self, session):
        repo = EmailDraftRepository(session)
        with pytest.raises(ValueError, match="status must be one of"):
            await repo.update_email_draft_status_by_id(1, "sent")
