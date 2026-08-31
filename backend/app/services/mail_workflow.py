"""Application services for mail analysis, versioned reply drafts and review.

The services intentionally stop before SMTP.  An analyser and draft generator
can propose content, but only a caller acting as a human can move a draft into
the ``approved`` state.  Every content edit and review action appends a new
immutable version through the storage port.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from app.ports.mail_workflow import (
    ArchivedMailContextReader,
    DraftVersionConflictError,
    MailAnalysisStore,
    MailAnalyzer,
    MailWorkflowError,
    ReplyDraftGenerator,
    ReplyDraftStore,
)
from app.schemas.mail_query import MailContext
from app.schemas.mail_workflow import (
    DraftDecision,
    DraftRevisionRequest,
    DraftStatus,
    DraftTransition,
    DraftTransitionKind,
    EmailAnalysis,
    EmailAnalysisProposal,
    ReplyDraft,
    ReplyDraftProposal,
)

Clock = Callable[[], datetime]
IdentifierFactory = Callable[[], str]


class ArchivedEmailNotFoundError(MailWorkflowError, LookupError):
    """Raised without revealing whether a missing email belongs to another account."""


class AnalysisNotFoundError(MailWorkflowError, LookupError):
    """Raised when an analysis is missing or outside the caller's account scope."""


class ReplyDraftNotFoundError(MailWorkflowError, LookupError):
    """Raised when a draft is missing or outside the caller's account scope."""


class AnalysisEmailMismatchError(MailWorkflowError):
    """Raised when a draft request joins analysis and mail from different sources."""


class DraftStateTransitionError(MailWorkflowError):
    """Raised when a requested human review action is illegal for the current state."""

    def __init__(self, *, status: DraftStatus, decision: DraftDecision) -> None:
        self.status = status
        self.decision = decision
        super().__init__(f"cannot {decision.value} a draft in {status.value} state")


class DraftRevisionNotAllowedError(MailWorkflowError):
    """Raised when a draft is not in an editable state for a new content version."""

    def __init__(self, *, status: DraftStatus) -> None:
        self.status = status
        super().__init__(f"cannot revise a draft in {status.value} state")


class DraftStoreContractError(MailWorkflowError, RuntimeError):
    """Raised when a storage adapter violates an immutable-version contract."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_identifier() -> str:
    return uuid4().hex


def _validate_email_id(email_id: int) -> None:
    if type(email_id) is not int or email_id <= 0:
        raise ValueError("email_id must be a positive int")


def _validate_expected_version(expected_version: int) -> None:
    if type(expected_version) is not int or expected_version <= 0:
        raise ValueError("expected_version must be a positive int")


def _normalize_actor_id(actor_id: str) -> str:
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("actor_id must be a non-empty string")
    normalized = actor_id.strip()
    if len(normalized) > 128:
        raise ValueError("actor_id must be at most 128 characters")
    return normalized


def _normalize_identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 128:
        raise ValueError(f"{name} must be at most 128 characters")
    return normalized


def _validate_scope(allowed_account_ids: frozenset[int]) -> frozenset[int]:
    if not isinstance(allowed_account_ids, frozenset):
        raise TypeError("allowed_account_ids must be a frozenset")
    if any(type(account_id) is not int or account_id <= 0 for account_id in allowed_account_ids):
        raise ValueError("allowed_account_ids must contain positive integers")
    return allowed_account_ids


def _validate_analysis_for_mail(analysis: EmailAnalysis, mail: MailContext) -> None:
    if analysis.email_id != mail.email_id or analysis.account_id != mail.account_id:
        raise AnalysisEmailMismatchError(
            "analysis must belong to the same archived email and account as the draft"
        )


def _require_context(
    context: MailContext | None,
    *,
    email_id: int,
) -> MailContext:
    if context is None:
        raise ArchivedEmailNotFoundError(f"archived email {email_id} is unavailable")
    return context


def _require_analysis(analysis: EmailAnalysis | None, *, analysis_id: str) -> EmailAnalysis:
    if analysis is None:
        raise AnalysisNotFoundError(f"analysis {analysis_id} is unavailable")
    return analysis


def _require_draft(draft: ReplyDraft | None, *, draft_id: str) -> ReplyDraft:
    if draft is None:
        raise ReplyDraftNotFoundError(f"reply draft {draft_id} is unavailable")
    return draft


class MailAnalysisService:
    """Analyze an accessible archived mail and persist the typed result.

    The injected ``MailAnalyzer`` may be backed by an LLM, but the LLM can only
    return :class:`EmailAnalysisProposal`; it never receives an analysis ID,
    account scope or a persistence capability.
    """

    def __init__(
        self,
        mail_reader: ArchivedMailContextReader,
        analyzer: MailAnalyzer,
        store: MailAnalysisStore,
        *,
        clock: Clock = _utc_now,
        identifier_factory: IdentifierFactory = _new_identifier,
    ) -> None:
        self._mail_reader = mail_reader
        self._analyzer = analyzer
        self._store = store
        self._clock = clock
        self._identifier_factory = identifier_factory

    async def analyze(
        self,
        email_id: int,
        *,
        allowed_account_ids: frozenset[int],
    ) -> EmailAnalysis:
        """Create one analysis for a scoped, already archived email."""

        _validate_email_id(email_id)
        scope = _validate_scope(allowed_account_ids)
        mail = _require_context(
            await self._mail_reader.get_context(email_id, allowed_account_ids=scope),
            email_id=email_id,
        )
        proposal = await self._analyzer.analyze(mail)
        if not isinstance(proposal, EmailAnalysisProposal):
            raise TypeError("MailAnalyzer must return EmailAnalysisProposal")
        analysis = EmailAnalysis.from_proposal(
            analysis_id=self._identifier_factory(),
            email_id=mail.email_id,
            account_id=mail.account_id,
            proposal=proposal,
            analyzed_at=self._clock(),
        )
        stored = await self._store.save(analysis)
        self._assert_saved_analysis(expected=analysis, actual=stored)
        return stored

    @staticmethod
    def _assert_saved_analysis(*, expected: EmailAnalysis, actual: EmailAnalysis) -> None:
        if not isinstance(actual, EmailAnalysis):
            raise DraftStoreContractError("MailAnalysisStore.save must return EmailAnalysis")
        if actual != expected:
            raise DraftStoreContractError("MailAnalysisStore.save changed an immutable analysis")


class ReplyDraftService:
    """Create, revise and explicitly review drafts without any send capability."""

    def __init__(
        self,
        mail_reader: ArchivedMailContextReader,
        analysis_store: MailAnalysisStore,
        generator: ReplyDraftGenerator,
        draft_store: ReplyDraftStore,
        *,
        clock: Clock = _utc_now,
        identifier_factory: IdentifierFactory = _new_identifier,
    ) -> None:
        self._mail_reader = mail_reader
        self._analysis_store = analysis_store
        self._generator = generator
        self._draft_store = draft_store
        self._clock = clock
        self._identifier_factory = identifier_factory

    async def create(
        self,
        email_id: int,
        analysis_id: str,
        *,
        actor_id: str,
        allowed_account_ids: frozenset[int],
    ) -> ReplyDraft:
        """Generate and store the first unapproved version of a reply draft."""

        _validate_email_id(email_id)
        analysis_id = _normalize_identifier("analysis_id", analysis_id)
        actor_id = _normalize_actor_id(actor_id)
        scope = _validate_scope(allowed_account_ids)
        mail = await self._get_mail(email_id, scope)
        analysis = await self._get_analysis(analysis_id, scope)
        _validate_analysis_for_mail(analysis, mail)
        proposal = await self._generator.generate(
            mail,
            analysis,
            previous_draft=None,
            instruction=None,
        )
        if not isinstance(proposal, ReplyDraftProposal):
            raise TypeError("ReplyDraftGenerator must return ReplyDraftProposal")

        now = self._clock()
        draft = ReplyDraft.from_proposal(
            draft_id=self._identifier_factory(),
            email_id=mail.email_id,
            account_id=mail.account_id,
            analysis_id=analysis.analysis_id,
            proposal=proposal,
            created_at=now,
            created_by=actor_id,
        )
        transition = DraftTransition(
            draft_id=draft.draft_id,
            from_version=None,
            to_version=draft.version,
            from_status=None,
            to_status=draft.status,
            kind=DraftTransitionKind.CREATED,
            actor_id=actor_id,
            occurred_at=now,
        )
        stored = await self._draft_store.create(draft, transition)
        self._assert_written_draft(expected=draft, actual=stored)
        return stored

    async def revise(
        self,
        draft_id: str,
        revision: DraftRevisionRequest,
        *,
        actor_id: str,
        expected_version: int,
        allowed_account_ids: frozenset[int],
    ) -> ReplyDraft:
        """Append an unapproved revision to a draft or rejected draft version."""

        draft_id = _normalize_identifier("draft_id", draft_id)
        if not isinstance(revision, DraftRevisionRequest):
            raise TypeError("revision must be a DraftRevisionRequest")
        actor_id = _normalize_actor_id(actor_id)
        _validate_expected_version(expected_version)
        scope = _validate_scope(allowed_account_ids)
        current = await self._get_current(draft_id, scope)
        self._assert_expected_version(current, expected_version)
        if current.status not in {DraftStatus.DRAFT, DraftStatus.REJECTED}:
            raise DraftRevisionNotAllowedError(status=current.status)

        mail = await self._get_mail(current.email_id, scope)
        analysis = await self._get_analysis(current.analysis_id, scope)
        _validate_analysis_for_mail(analysis, mail)
        proposal = await self._generator.generate(
            mail,
            analysis,
            previous_draft=current,
            instruction=revision.instruction,
        )
        if not isinstance(proposal, ReplyDraftProposal):
            raise TypeError("ReplyDraftGenerator must return ReplyDraftProposal")

        now = self._clock()
        revised = replace(
            current,
            version=current.version + 1,
            status=DraftStatus.DRAFT,
            recipients=proposal.recipients,
            subject=proposal.subject,
            body_text=proposal.body_text,
            updated_at=now,
            updated_by=actor_id,
            reviewed_by=None,
            reviewed_at=None,
            review_comment=None,
        )
        transition = DraftTransition(
            draft_id=current.draft_id,
            from_version=current.version,
            to_version=revised.version,
            from_status=current.status,
            to_status=revised.status,
            kind=DraftTransitionKind.REVISED,
            actor_id=actor_id,
            occurred_at=now,
            comment=revision.instruction,
        )
        stored = await self._draft_store.append_revision(
            revised,
            transition,
            expected_version=current.version,
        )
        self._assert_written_draft(expected=revised, actual=stored)
        return stored

    async def decide(
        self,
        draft_id: str,
        decision: DraftDecision,
        *,
        actor_id: str,
        expected_version: int,
        allowed_account_ids: frozenset[int],
        comment: str | None = None,
    ) -> ReplyDraft:
        """Append one explicit human-review state change to the current version."""

        draft_id = _normalize_identifier("draft_id", draft_id)
        if not isinstance(decision, DraftDecision):
            raise TypeError("decision must be a DraftDecision")
        actor_id = _normalize_actor_id(actor_id)
        _validate_expected_version(expected_version)
        scope = _validate_scope(allowed_account_ids)
        current = await self._get_current(draft_id, scope)
        self._assert_expected_version(current, expected_version)
        target_status, transition_kind = self._transition_for(current.status, decision)

        now = self._clock()
        reviewed = target_status in {DraftStatus.APPROVED, DraftStatus.REJECTED}
        decided = replace(
            current,
            version=current.version + 1,
            status=target_status,
            updated_at=now,
            updated_by=actor_id,
            reviewed_by=actor_id if reviewed else None,
            reviewed_at=now if reviewed else None,
            review_comment=comment if reviewed else None,
        )
        transition = DraftTransition(
            draft_id=current.draft_id,
            from_version=current.version,
            to_version=decided.version,
            from_status=current.status,
            to_status=decided.status,
            kind=transition_kind,
            actor_id=actor_id,
            occurred_at=now,
            comment=comment,
        )
        stored = await self._draft_store.append_revision(
            decided,
            transition,
            expected_version=current.version,
        )
        self._assert_written_draft(expected=decided, actual=stored)
        return stored

    async def get_current(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> ReplyDraft:
        """Return the current scoped draft version or a non-disclosing not-found error."""

        draft_id = _normalize_identifier("draft_id", draft_id)
        return await self._get_current(draft_id, _validate_scope(allowed_account_ids))

    async def list_versions(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[ReplyDraft]:
        """Return immutable versions in order after validating the store contract."""

        draft_id = _normalize_identifier("draft_id", draft_id)
        versions = await self._draft_store.list_versions(
            draft_id,
            allowed_account_ids=_validate_scope(allowed_account_ids),
        )
        if not versions:
            raise ReplyDraftNotFoundError(f"reply draft {draft_id} is unavailable")
        expected_versions = list(range(1, len(versions) + 1))
        if [draft.version for draft in versions] != expected_versions:
            raise DraftStoreContractError("draft versions must be contiguous and ascending")
        if any(draft.draft_id != draft_id for draft in versions):
            raise DraftStoreContractError("draft history contains an unrelated draft")
        return versions

    async def _get_mail(
        self,
        email_id: int,
        scope: frozenset[int],
    ) -> MailContext:
        return _require_context(
            await self._mail_reader.get_context(email_id, allowed_account_ids=scope),
            email_id=email_id,
        )

    async def _get_analysis(
        self,
        analysis_id: str,
        scope: frozenset[int],
    ) -> EmailAnalysis:
        return _require_analysis(
            await self._analysis_store.get(analysis_id, allowed_account_ids=scope),
            analysis_id=analysis_id,
        )

    async def _get_current(
        self,
        draft_id: str,
        scope: frozenset[int],
    ) -> ReplyDraft:
        return _require_draft(
            await self._draft_store.get_current(draft_id, allowed_account_ids=scope),
            draft_id=draft_id,
        )

    @staticmethod
    def _assert_expected_version(draft: ReplyDraft, expected_version: int) -> None:
        if draft.version != expected_version:
            raise DraftVersionConflictError(
                f"draft {draft.draft_id} is at version {draft.version}, not {expected_version}"
            )

    @staticmethod
    def _transition_for(
        status: DraftStatus,
        decision: DraftDecision,
    ) -> tuple[DraftStatus, DraftTransitionKind]:
        transitions: dict[
            tuple[DraftStatus, DraftDecision],
            tuple[DraftStatus, DraftTransitionKind],
        ] = {
            (DraftStatus.DRAFT, DraftDecision.SUBMIT_FOR_REVIEW): (
                DraftStatus.PENDING_REVIEW,
                DraftTransitionKind.SUBMITTED_FOR_REVIEW,
            ),
            (DraftStatus.PENDING_REVIEW, DraftDecision.APPROVE): (
                DraftStatus.APPROVED,
                DraftTransitionKind.APPROVED,
            ),
            (DraftStatus.PENDING_REVIEW, DraftDecision.REJECT): (
                DraftStatus.REJECTED,
                DraftTransitionKind.REJECTED,
            ),
            (DraftStatus.PENDING_REVIEW, DraftDecision.WITHDRAW): (
                DraftStatus.DRAFT,
                DraftTransitionKind.WITHDRAWN,
            ),
        }
        transition = transitions.get((status, decision))
        if transition is None:
            raise DraftStateTransitionError(status=status, decision=decision)
        return transition

    @staticmethod
    def _assert_written_draft(*, expected: ReplyDraft, actual: ReplyDraft) -> None:
        if not isinstance(actual, ReplyDraft):
            raise DraftStoreContractError("ReplyDraftStore must return ReplyDraft")
        if actual != expected:
            raise DraftStoreContractError("ReplyDraftStore changed an immutable draft version")
