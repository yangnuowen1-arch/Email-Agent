"""Unit tests for typed mail analysis, draft revisions and human review."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

import pytest

from app.ports import DraftVersionConflictError
from app.schemas import (
    DraftDecision,
    DraftRevisionRequest,
    DraftStatus,
    DraftTransition,
    EmailAnalysis,
    EmailAnalysisProposal,
    MailContext,
    MailIntent,
    MailUrgency,
    ReplyDraft,
    ReplyDraftProposal,
)
from app.services import (
    AnalysisEmailMismatchError,
    AnalysisNotFoundError,
    ArchivedEmailNotFoundError,
    DraftRevisionNotAllowedError,
    DraftStateTransitionError,
    MailAnalysisService,
    ReplyDraftService,
)


class FakeMailReader:
    def __init__(self, contexts: list[MailContext]) -> None:
        self.contexts = {context.email_id: context for context in contexts}
        self.calls: list[tuple[int, frozenset[int]]] = []

    async def get_context(
        self,
        email_id: int,
        *,
        allowed_account_ids: frozenset[int],
    ) -> MailContext | None:
        self.calls.append((email_id, allowed_account_ids))
        context = self.contexts.get(email_id)
        if context is None or context.account_id not in allowed_account_ids:
            return None
        return context


class FakeAnalyzer:
    def __init__(self, proposal: EmailAnalysisProposal) -> None:
        self.proposal = proposal
        self.calls: list[MailContext] = []

    async def analyze(self, mail: MailContext) -> EmailAnalysisProposal:
        self.calls.append(mail)
        return self.proposal


class FakeAnalysisStore:
    def __init__(self) -> None:
        self.items: dict[str, EmailAnalysis] = {}
        self.saved: list[EmailAnalysis] = []

    async def save(self, analysis: EmailAnalysis) -> EmailAnalysis:
        self.items[analysis.analysis_id] = analysis
        self.saved.append(analysis)
        return analysis

    async def get(
        self,
        analysis_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> EmailAnalysis | None:
        analysis = self.items.get(analysis_id)
        if analysis is None or analysis.account_id not in allowed_account_ids:
            return None
        return analysis


class FakeDraftGenerator:
    def __init__(self, proposals: list[ReplyDraftProposal]) -> None:
        self._proposals = deque(proposals)
        self.calls: list[tuple[MailContext, EmailAnalysis, ReplyDraft | None, str | None]] = []

    async def generate(
        self,
        mail: MailContext,
        analysis: EmailAnalysis,
        *,
        previous_draft: ReplyDraft | None,
        instruction: str | None,
    ) -> ReplyDraftProposal:
        self.calls.append((mail, analysis, previous_draft, instruction))
        return self._proposals.popleft()


class FakeDraftStore:
    def __init__(self) -> None:
        self.versions: dict[str, list[ReplyDraft]] = {}
        self.transitions: list[DraftTransition] = []
        self.force_conflict = False

    async def create(self, draft: ReplyDraft, transition: DraftTransition) -> ReplyDraft:
        assert draft.version == 1
        self.versions[draft.draft_id] = [draft]
        self.transitions.append(transition)
        return draft

    async def get_current(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> ReplyDraft | None:
        history = self.versions.get(draft_id)
        if not history or history[-1].account_id not in allowed_account_ids:
            return None
        return history[-1]

    async def list_versions(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[ReplyDraft]:
        history = self.versions.get(draft_id, [])
        if not history or history[-1].account_id not in allowed_account_ids:
            return []
        return list(history)

    async def append_revision(
        self,
        draft: ReplyDraft,
        transition: DraftTransition,
        *,
        expected_version: int,
    ) -> ReplyDraft:
        history = self.versions.get(draft.draft_id)
        if self.force_conflict or not history or history[-1].version != expected_version:
            raise DraftVersionConflictError("simulated optimistic-lock conflict")
        history.append(draft)
        self.transitions.append(transition)
        return draft


class IncrementingClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(seconds=1)
        return value


class SequentialIds:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._prefix}-{self._value}"


def _mail(*, email_id: int = 11, account_id: int = 1) -> MailContext:
    return MailContext(
        email_id=email_id,
        account_id=account_id,
        uid=42,
        message_id="<message@example.com>",
        subject="Pricing question",
        sender="customer@example.com",
        recipients=("sales@example.com",),
        sent_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        text_body="Could you confirm the annual price and implementation schedule?",
        fetched_at=datetime(2026, 8, 30, 9, 1, tzinfo=UTC),
    )


def _analysis_proposal() -> EmailAnalysisProposal:
    return EmailAnalysisProposal(
        summary="Customer requests annual pricing and an implementation timeline.",
        intent=MailIntent.REQUEST,
        urgency=MailUrgency.HIGH,
        reply_required=True,
        key_points=("Annual pricing", "Implementation timeline"),
        action_items=("Prepare a commercial reply",),
    )


def _draft_proposal(
    *, body: str = "Thanks for reaching out. We will send pricing shortly."
) -> ReplyDraftProposal:
    return ReplyDraftProposal(
        recipients=("customer@example.com",),
        subject="Re: Pricing question",
        body_text=body,
    )


async def _analyze(
    *,
    reader: FakeMailReader,
    analysis_store: FakeAnalysisStore,
    clock: IncrementingClock,
) -> EmailAnalysis:
    service = MailAnalysisService(
        reader,
        FakeAnalyzer(_analysis_proposal()),
        analysis_store,
        clock=clock,
        identifier_factory=SequentialIds("analysis"),
    )
    return await service.analyze(11, allowed_account_ids=frozenset({1}))


async def test_analysis_then_draft_submission_and_approval_append_auditable_versions() -> None:
    reader = FakeMailReader([_mail()])
    analysis_store = FakeAnalysisStore()
    draft_store = FakeDraftStore()
    clock = IncrementingClock()
    analysis = await _analyze(reader=reader, analysis_store=analysis_store, clock=clock)
    generator = FakeDraftGenerator([_draft_proposal()])
    drafts = ReplyDraftService(
        reader,
        analysis_store,
        generator,
        draft_store,
        clock=clock,
        identifier_factory=SequentialIds("draft"),
    )

    created = await drafts.create(
        11,
        analysis.analysis_id,
        actor_id="assistant-user",
        allowed_account_ids=frozenset({1}),
    )
    submitted = await drafts.decide(
        created.draft_id,
        DraftDecision.SUBMIT_FOR_REVIEW,
        actor_id="assistant-user",
        expected_version=created.version,
        allowed_account_ids=frozenset({1}),
    )
    approved = await drafts.decide(
        submitted.draft_id,
        DraftDecision.APPROVE,
        actor_id="human-reviewer",
        expected_version=submitted.version,
        allowed_account_ids=frozenset({1}),
        comment="Tone and commitments verified.",
    )

    assert analysis.email_id == 11
    assert analysis.account_id == 1
    assert analysis_store.saved == [analysis]
    assert created.status is DraftStatus.DRAFT
    assert submitted.status is DraftStatus.PENDING_REVIEW
    assert approved.status is DraftStatus.APPROVED
    assert approved.version == 3
    assert approved.reviewed_by == "human-reviewer"
    assert approved.review_comment == "Tone and commitments verified."
    assert created.body_text == approved.body_text
    assert generator.calls == [(_mail(), analysis, None, None)]
    assert [transition.kind.value for transition in draft_store.transitions] == [
        "created",
        "submitted_for_review",
        "approved",
    ]
    history = await drafts.list_versions(approved.draft_id, allowed_account_ids=frozenset({1}))
    assert [draft.version for draft in history] == [1, 2, 3]
    assert [draft.status for draft in history] == [
        DraftStatus.DRAFT,
        DraftStatus.PENDING_REVIEW,
        DraftStatus.APPROVED,
    ]


async def test_rejection_can_be_revised_into_a_new_unapproved_version() -> None:
    reader = FakeMailReader([_mail()])
    analysis_store = FakeAnalysisStore()
    draft_store = FakeDraftStore()
    clock = IncrementingClock()
    analysis = await _analyze(reader=reader, analysis_store=analysis_store, clock=clock)
    generator = FakeDraftGenerator(
        [
            _draft_proposal(body="We can share pricing this week."),
            _draft_proposal(body="We can share pricing tomorrow and schedule a call."),
        ]
    )
    drafts = ReplyDraftService(
        reader,
        analysis_store,
        generator,
        draft_store,
        clock=clock,
        identifier_factory=SequentialIds("draft"),
    )
    created = await drafts.create(
        11,
        analysis.analysis_id,
        actor_id="author",
        allowed_account_ids=frozenset({1}),
    )
    submitted = await drafts.decide(
        created.draft_id,
        DraftDecision.SUBMIT_FOR_REVIEW,
        actor_id="author",
        expected_version=1,
        allowed_account_ids=frozenset({1}),
    )
    rejected = await drafts.decide(
        created.draft_id,
        DraftDecision.REJECT,
        actor_id="reviewer",
        expected_version=submitted.version,
        allowed_account_ids=frozenset({1}),
        comment="Please make the commitment date specific.",
    )

    revised = await drafts.revise(
        created.draft_id,
        DraftRevisionRequest(instruction="Commit to a delivery date."),
        actor_id="author",
        expected_version=rejected.version,
        allowed_account_ids=frozenset({1}),
    )

    assert rejected.status is DraftStatus.REJECTED
    assert rejected.reviewed_by == "reviewer"
    assert revised.status is DraftStatus.DRAFT
    assert revised.version == 4
    assert revised.reviewed_by is None
    assert revised.body_text == "We can share pricing tomorrow and schedule a call."
    assert generator.calls[-1][2] == rejected
    assert generator.calls[-1][3] == "Commit to a delivery date."
    assert draft_store.transitions[-1].kind.value == "revised"
    assert draft_store.transitions[-1].from_status is DraftStatus.REJECTED


async def test_only_legal_review_transitions_are_accepted_and_stale_writes_fail() -> None:
    reader = FakeMailReader([_mail()])
    analysis_store = FakeAnalysisStore()
    draft_store = FakeDraftStore()
    clock = IncrementingClock()
    analysis = await _analyze(reader=reader, analysis_store=analysis_store, clock=clock)
    generator = FakeDraftGenerator([_draft_proposal()])
    drafts = ReplyDraftService(
        reader,
        analysis_store,
        generator,
        draft_store,
        clock=clock,
        identifier_factory=SequentialIds("draft"),
    )
    created = await drafts.create(
        11,
        analysis.analysis_id,
        actor_id="author",
        allowed_account_ids=frozenset({1}),
    )

    with pytest.raises(DraftStateTransitionError, match="cannot approve"):
        await drafts.decide(
            created.draft_id,
            DraftDecision.APPROVE,
            actor_id="reviewer",
            expected_version=created.version,
            allowed_account_ids=frozenset({1}),
        )

    submitted = await drafts.decide(
        created.draft_id,
        DraftDecision.SUBMIT_FOR_REVIEW,
        actor_id="author",
        expected_version=created.version,
        allowed_account_ids=frozenset({1}),
    )
    with pytest.raises(DraftRevisionNotAllowedError, match="cannot revise"):
        await drafts.revise(
            created.draft_id,
            DraftRevisionRequest(instruction="Use a warmer greeting."),
            actor_id="author",
            expected_version=submitted.version,
            allowed_account_ids=frozenset({1}),
        )
    with pytest.raises(DraftVersionConflictError, match="version 2, not 1"):
        await drafts.decide(
            created.draft_id,
            DraftDecision.WITHDRAW,
            actor_id="author",
            expected_version=1,
            allowed_account_ids=frozenset({1}),
        )
    assert len(draft_store.transitions) == 2


async def test_store_compare_and_swap_conflict_propagates_without_a_hidden_retry() -> None:
    reader = FakeMailReader([_mail()])
    analysis_store = FakeAnalysisStore()
    draft_store = FakeDraftStore()
    clock = IncrementingClock()
    analysis = await _analyze(reader=reader, analysis_store=analysis_store, clock=clock)
    drafts = ReplyDraftService(
        reader,
        analysis_store,
        FakeDraftGenerator([_draft_proposal()]),
        draft_store,
        clock=clock,
        identifier_factory=SequentialIds("draft"),
    )
    created = await drafts.create(
        11,
        analysis.analysis_id,
        actor_id="author",
        allowed_account_ids=frozenset({1}),
    )
    draft_store.force_conflict = True

    with pytest.raises(DraftVersionConflictError, match="simulated"):
        await drafts.decide(
            created.draft_id,
            DraftDecision.SUBMIT_FOR_REVIEW,
            actor_id="author",
            expected_version=created.version,
            allowed_account_ids=frozenset({1}),
        )
    assert len(draft_store.transitions) == 1


async def test_analysis_and_draft_requests_preserve_scope_and_reject_mismatched_analysis() -> None:
    reader = FakeMailReader([_mail(), _mail(email_id=12, account_id=2)])
    analysis_store = FakeAnalysisStore()
    clock = IncrementingClock()
    analyzer = FakeAnalyzer(_analysis_proposal())
    analysis_service = MailAnalysisService(
        reader,
        analyzer,
        analysis_store,
        clock=clock,
        identifier_factory=SequentialIds("analysis"),
    )

    with pytest.raises(ArchivedEmailNotFoundError):
        await analysis_service.analyze(12, allowed_account_ids=frozenset({1}))
    assert analyzer.calls == []

    foreign_analysis = EmailAnalysis.from_proposal(
        analysis_id="foreign-analysis",
        email_id=12,
        account_id=2,
        proposal=_analysis_proposal(),
        analyzed_at=clock(),
    )
    analysis_store.items[foreign_analysis.analysis_id] = foreign_analysis
    drafts = ReplyDraftService(
        reader,
        analysis_store,
        FakeDraftGenerator([_draft_proposal()]),
        FakeDraftStore(),
        clock=clock,
        identifier_factory=SequentialIds("draft"),
    )
    with pytest.raises(AnalysisNotFoundError):
        await drafts.create(
            11,
            foreign_analysis.analysis_id,
            actor_id="author",
            allowed_account_ids=frozenset({1}),
        )

    mismatched_analysis = EmailAnalysis.from_proposal(
        analysis_id="mismatched-analysis",
        email_id=12,
        account_id=1,
        proposal=_analysis_proposal(),
        analyzed_at=clock(),
    )
    analysis_store.items[mismatched_analysis.analysis_id] = mismatched_analysis
    with pytest.raises(AnalysisEmailMismatchError):
        await drafts.create(
            11,
            mismatched_analysis.analysis_id,
            actor_id="author",
            allowed_account_ids=frozenset({1}),
        )
