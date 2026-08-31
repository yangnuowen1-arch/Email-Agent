"""SQLAlchemy adapters for the mail-analysis and reply-draft workflow ports."""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.db import EmailAnalysisRecord, ReplyDraftTransitionRecord, ReplyDraftVersionRecord
from app.db.engine import Database
from app.db.mail_workflow_repositories import MailWorkflowRepository
from app.ports.mail_workflow import DraftVersionConflictError, MailAnalysisStore, ReplyDraftStore
from app.schemas.mail_workflow import (
    DraftStatus,
    DraftTransition,
    EmailAnalysis,
    MailIntent,
    MailUrgency,
    ReplyDraft,
)


def _as_aware(value: datetime) -> datetime:
    """Normalize dialects such as SQLite that lose timezone info on readback."""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_analysis_record(analysis: EmailAnalysis) -> EmailAnalysisRecord:
    return EmailAnalysisRecord(
        analysis_id=analysis.analysis_id,
        email_id=analysis.email_id,
        account_id=analysis.account_id,
        summary=analysis.summary,
        intent=analysis.intent.value,
        urgency=analysis.urgency.value,
        reply_required=analysis.reply_required,
        key_points=list(analysis.key_points),
        action_items=list(analysis.action_items),
        analyzed_at=analysis.analyzed_at,
    )


def _to_analysis(record: EmailAnalysisRecord) -> EmailAnalysis:
    return EmailAnalysis(
        analysis_id=record.analysis_id,
        email_id=record.email_id,
        account_id=record.account_id,
        summary=record.summary,
        intent=MailIntent(record.intent),
        urgency=MailUrgency(record.urgency),
        reply_required=record.reply_required,
        key_points=tuple(record.key_points or ()),
        action_items=tuple(record.action_items or ()),
        analyzed_at=_as_aware(record.analyzed_at),
    )


def _to_draft_record(draft: ReplyDraft) -> ReplyDraftVersionRecord:
    return ReplyDraftVersionRecord(
        draft_id=draft.draft_id,
        version=draft.version,
        email_id=draft.email_id,
        account_id=draft.account_id,
        analysis_id=draft.analysis_id,
        status=draft.status.value,
        recipients=list(draft.recipients),
        subject=draft.subject,
        body_text=draft.body_text,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        created_by=draft.created_by,
        updated_by=draft.updated_by,
        reviewed_by=draft.reviewed_by,
        reviewed_at=draft.reviewed_at,
        review_comment=draft.review_comment,
    )


def _to_draft(record: ReplyDraftVersionRecord) -> ReplyDraft:
    return ReplyDraft(
        draft_id=record.draft_id,
        version=record.version,
        email_id=record.email_id,
        account_id=record.account_id,
        analysis_id=record.analysis_id,
        status=DraftStatus(record.status),
        recipients=tuple(record.recipients or ()),
        subject=record.subject,
        body_text=record.body_text,
        created_at=_as_aware(record.created_at),
        updated_at=_as_aware(record.updated_at),
        created_by=record.created_by,
        updated_by=record.updated_by,
        reviewed_by=record.reviewed_by,
        reviewed_at=_as_aware(record.reviewed_at) if record.reviewed_at is not None else None,
        review_comment=record.review_comment,
    )


def _to_transition_record(transition: DraftTransition) -> ReplyDraftTransitionRecord:
    return ReplyDraftTransitionRecord(
        draft_id=transition.draft_id,
        from_version=transition.from_version,
        to_version=transition.to_version,
        from_status=transition.from_status.value if transition.from_status is not None else None,
        to_status=transition.to_status.value,
        kind=transition.kind.value,
        actor_id=transition.actor_id,
        occurred_at=transition.occurred_at,
        comment=transition.comment,
    )


def _validate_transition_matches_draft(draft: ReplyDraft, transition: DraftTransition) -> None:
    """Defend the atomic persistence boundary against mismatched audit rows."""

    if (
        transition.draft_id != draft.draft_id
        or transition.to_version != draft.version
        or transition.to_status is not draft.status
    ):
        raise ValueError("draft transition must describe the exact draft version being stored")


class SqlAlchemyMailWorkflowStore(MailAnalysisStore, ReplyDraftStore):
    """Persist workflow state in transactions owned by :class:`Database`.

    Analyses are independent immutable records.  Draft creation and every
    subsequent version append write their corresponding review transition in
    the same database transaction so an approval event can never exist without
    the exact approved content (or vice versa).
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def save(self, analysis: EmailAnalysis) -> EmailAnalysis:
        async with self._database.session() as session:
            record = await MailWorkflowRepository(session).create_analysis(
                _to_analysis_record(analysis)
            )
        return _to_analysis(record)

    async def get(
        self,
        analysis_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> EmailAnalysis | None:
        async with self._database.session() as session:
            record = await MailWorkflowRepository(session).get_analysis_in_accounts(
                analysis_id,
                allowed_account_ids=allowed_account_ids,
            )
        return _to_analysis(record) if record is not None else None

    async def create(self, draft: ReplyDraft, transition: DraftTransition) -> ReplyDraft:
        _validate_transition_matches_draft(draft, transition)
        async with self._database.session() as session:
            record = await MailWorkflowRepository(session).create_draft_version(
                _to_draft_record(draft),
                _to_transition_record(transition),
            )
        return _to_draft(record)

    async def get_current(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> ReplyDraft | None:
        async with self._database.session() as session:
            record = await MailWorkflowRepository(session).get_current_draft_in_accounts(
                draft_id,
                allowed_account_ids=allowed_account_ids,
            )
        return _to_draft(record) if record is not None else None

    async def list_versions(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[ReplyDraft]:
        async with self._database.session() as session:
            records = await MailWorkflowRepository(session).list_draft_versions_in_accounts(
                draft_id,
                allowed_account_ids=allowed_account_ids,
            )
        return [_to_draft(record) for record in records]

    async def append_revision(
        self,
        draft: ReplyDraft,
        transition: DraftTransition,
        *,
        expected_version: int,
    ) -> ReplyDraft:
        _validate_transition_matches_draft(draft, transition)
        if draft.version != expected_version + 1 or transition.from_version != expected_version:
            raise DraftVersionConflictError(
                "appended draft version does not follow expected_version"
            )
        async with self._database.session() as session:
            record = await MailWorkflowRepository(session).append_draft_version(
                _to_draft_record(draft),
                _to_transition_record(transition),
                expected_version=expected_version,
            )
        return _to_draft(record)
