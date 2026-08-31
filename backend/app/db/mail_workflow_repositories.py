"""SQLAlchemy repositories for durable mail analyses and reply-draft review history."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db import EmailAnalysisRecord, ReplyDraftTransitionRecord, ReplyDraftVersionRecord
from app.ports.mail_workflow import DraftVersionConflictError


class MailWorkflowRepository:
    """Persist workflow records without owning the surrounding transaction.

    A :class:`~app.db.engine.Database` session is deliberately supplied by the
    adapter.  This keeps a draft version and its matching audit transition in
    the same commit, while leaving application services independent of
    SQLAlchemy.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_analysis(self, record: EmailAnalysisRecord) -> EmailAnalysisRecord:
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_analysis_in_accounts(
        self,
        analysis_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> EmailAnalysisRecord | None:
        if not allowed_account_ids:
            return None
        stmt = select(EmailAnalysisRecord).where(
            EmailAnalysisRecord.analysis_id == analysis_id,
            EmailAnalysisRecord.account_id.in_(allowed_account_ids),
        )
        return await self.session.scalar(stmt)

    async def create_draft_version(
        self,
        draft: ReplyDraftVersionRecord,
        transition: ReplyDraftTransitionRecord,
    ) -> ReplyDraftVersionRecord:
        """Write version one and its immutable ``created`` audit row together."""

        self.session.add(draft)
        self.session.add(transition)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise DraftVersionConflictError("reply draft already exists") from exc
        return draft

    async def get_current_draft_in_accounts(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> ReplyDraftVersionRecord | None:
        if not allowed_account_ids:
            return None
        stmt = (
            select(ReplyDraftVersionRecord)
            .where(
                ReplyDraftVersionRecord.draft_id == draft_id,
                ReplyDraftVersionRecord.account_id.in_(allowed_account_ids),
            )
            .order_by(desc(ReplyDraftVersionRecord.version))
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def list_draft_versions_in_accounts(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[ReplyDraftVersionRecord]:
        if not allowed_account_ids:
            return []
        stmt = (
            select(ReplyDraftVersionRecord)
            .where(
                ReplyDraftVersionRecord.draft_id == draft_id,
                ReplyDraftVersionRecord.account_id.in_(allowed_account_ids),
            )
            .order_by(ReplyDraftVersionRecord.version)
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def append_draft_version(
        self,
        draft: ReplyDraftVersionRecord,
        transition: ReplyDraftTransitionRecord,
        *,
        expected_version: int,
    ) -> ReplyDraftVersionRecord:
        """Append under a row lock so only one caller wins a draft revision.

        PostgreSQL locks the current version row before the append. A competing
        writer either observes the newer row and fails the compare-and-swap
        check, or loses the composite-primary-key race during ``flush``. Both
        paths become :class:`DraftVersionConflictError` rather than creating a
        forked history.
        """

        current_stmt = (
            select(ReplyDraftVersionRecord)
            .where(ReplyDraftVersionRecord.draft_id == draft.draft_id)
            .order_by(desc(ReplyDraftVersionRecord.version))
            .limit(1)
            .with_for_update()
        )
        current = await self.session.scalar(current_stmt)
        if current is None or current.version != expected_version:
            raise DraftVersionConflictError("reply draft version is no longer current")
        if (
            current.email_id != draft.email_id
            or current.account_id != draft.account_id
            or current.analysis_id != draft.analysis_id
        ):
            raise ValueError("reply draft identity cannot change across versions")

        self.session.add(draft)
        self.session.add(transition)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            # A competing writer can commit a new version between this
            # transaction's current-row read and insert. The composite primary
            # key is the final compare-and-swap guard.
            raise DraftVersionConflictError("reply draft version is no longer current") from exc
        return draft
