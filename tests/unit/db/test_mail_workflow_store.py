"""Persistence tests for analysis, immutable drafts and approval audit rows."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.db import EmailAnalysisRecord, ReplyDraftTransitionRecord, ReplyDraftVersionRecord
from app.db.engine import Database
from app.db.mail_workflow_store import SqlAlchemyMailWorkflowStore
from app.ports import DraftVersionConflictError
from app.schemas import (
    DraftStatus,
    DraftTransition,
    DraftTransitionKind,
    EmailAnalysis,
    EmailAnalysisProposal,
    MailIntent,
    MailUrgency,
    ReplyDraft,
    ReplyDraftProposal,
)


@pytest.fixture
async def workflow_store():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    tables = [
        EmailAnalysisRecord.__table__,
        ReplyDraftVersionRecord.__table__,
        ReplyDraftTransitionRecord.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: EmailAnalysisRecord.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    database = Database(engine=engine, sessions=async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield SqlAlchemyMailWorkflowStore(database), database
    finally:
        await database.dispose()


def _analysis() -> EmailAnalysis:
    return EmailAnalysis.from_proposal(
        analysis_id="analysis-1",
        email_id=9,
        account_id=1,
        proposal=EmailAnalysisProposal(
            summary="The customer asks for a price.",
            intent=MailIntent.REQUEST,
            urgency=MailUrgency.HIGH,
            reply_required=True,
            key_points=("Need annual price",),
            action_items=("Prepare quotation",),
        ),
        analyzed_at=datetime(2026, 8, 31, 9, tzinfo=UTC),
    )


def _draft() -> ReplyDraft:
    return ReplyDraft.from_proposal(
        draft_id="draft-1",
        email_id=9,
        account_id=1,
        analysis_id="analysis-1",
        proposal=ReplyDraftProposal(
            recipients=("customer@example.com",),
            subject="Re: Pricing",
            body_text="Thank you. We will send pricing shortly.",
        ),
        created_at=datetime(2026, 8, 31, 9, 1, tzinfo=UTC),
        created_by="author",
    )


def _created_transition(draft: ReplyDraft) -> DraftTransition:
    return DraftTransition(
        draft_id=draft.draft_id,
        from_version=None,
        to_version=1,
        from_status=None,
        to_status=DraftStatus.DRAFT,
        kind=DraftTransitionKind.CREATED,
        actor_id="author",
        occurred_at=draft.created_at,
    )


async def test_store_persists_analysis_and_enforces_account_scope(workflow_store) -> None:
    store, _database = workflow_store
    analysis = _analysis()

    stored = await store.save(analysis)

    assert stored == analysis
    assert await store.get(analysis.analysis_id, allowed_account_ids=frozenset({2})) is None
    accessible = await store.get(analysis.analysis_id, allowed_account_ids=frozenset({1}))
    assert accessible == analysis


async def test_store_appends_immutable_versions_with_a_matching_transition(workflow_store) -> None:
    store, database = workflow_store
    draft = _draft()
    created = await store.create(draft, _created_transition(draft))
    submitted = replace(
        created,
        version=2,
        status=DraftStatus.PENDING_REVIEW,
        updated_at=created.updated_at + timedelta(seconds=1),
        updated_by="author",
    )
    submitted_transition = DraftTransition(
        draft_id=draft.draft_id,
        from_version=1,
        to_version=2,
        from_status=DraftStatus.DRAFT,
        to_status=DraftStatus.PENDING_REVIEW,
        kind=DraftTransitionKind.SUBMITTED_FOR_REVIEW,
        actor_id="author",
        occurred_at=submitted.updated_at,
    )

    stored = await store.append_revision(submitted, submitted_transition, expected_version=1)

    assert stored == submitted
    assert await store.get_current(draft.draft_id, allowed_account_ids=frozenset({2})) is None
    assert await store.get_current(draft.draft_id, allowed_account_ids=frozenset({1})) == submitted
    versions = await store.list_versions(draft.draft_id, allowed_account_ids=frozenset({1}))
    assert versions == [created, submitted]

    async with database.session() as session:
        transition_count = await session.scalar(
            select(func.count()).select_from(ReplyDraftTransitionRecord)
        )
    assert transition_count == 2


async def test_store_rejects_a_stale_compare_and_swap_version(workflow_store) -> None:
    store, _database = workflow_store
    draft = _draft()
    await store.create(draft, _created_transition(draft))
    attempted = replace(
        draft,
        version=2,
        status=DraftStatus.PENDING_REVIEW,
        updated_at=draft.updated_at + timedelta(seconds=1),
    )
    transition = DraftTransition(
        draft_id=draft.draft_id,
        from_version=1,
        to_version=2,
        from_status=DraftStatus.DRAFT,
        to_status=DraftStatus.PENDING_REVIEW,
        kind=DraftTransitionKind.SUBMITTED_FOR_REVIEW,
        actor_id="author",
        occurred_at=attempted.updated_at,
    )

    with pytest.raises(DraftVersionConflictError):
        await store.append_revision(attempted, transition, expected_version=999)
