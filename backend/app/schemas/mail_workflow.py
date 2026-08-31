"""Pure contracts for analysing archived mail and reviewing reply drafts.

These records deliberately describe proposals and durable business state, not
ORM rows or a particular LLM provider payload.  The application services add
trusted identifiers, account ownership and audit information around proposals
returned by an analyser or draft generator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MailIntent(StrEnum):
    """High-level purpose assigned to an archived email."""

    ACTION_REQUIRED = "action_required"
    INFORMATIONAL = "informational"
    MEETING = "meeting"
    QUESTION = "question"
    REQUEST = "request"
    OTHER = "other"


class MailUrgency(StrEnum):
    """A bounded urgency value that callers can render or sort safely."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class DraftStatus(StrEnum):
    """The current human-review state of one immutable draft version."""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class DraftDecision(StrEnum):
    """Human actions that can move a reply draft through its review workflow."""

    SUBMIT_FOR_REVIEW = "submit_for_review"
    APPROVE = "approve"
    REJECT = "reject"
    WITHDRAW = "withdraw"


class DraftTransitionKind(StrEnum):
    """Audit-friendly description of a newly appended draft version."""

    CREATED = "created"
    REVISED = "revised"
    SUBMITTED_FOR_REVIEW = "submitted_for_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


def _require_identifier(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > 128:
        raise ValueError(f"{name} must be at most 128 characters")
    return normalized


def _require_text(name: str, value: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{name} must be at most {max_length} characters")
    return normalized


def _normalize_optional_text(name: str, value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    return _require_text(name, value, max_length=max_length)


def _normalize_text_items(name: str, value: tuple[str, ...], *, max_items: int) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{name} must be a tuple of strings")
    if len(value) > max_items:
        raise ValueError(f"{name} must contain at most {max_items} items")
    return tuple(_require_text(f"{name} item", item, max_length=1_000) for item in value)


def _require_positive_int(name: str, value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive int")
    return value


def _require_aware_datetime(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class EmailAnalysisProposal:
    """Structured model proposal before trusted mail identity is attached."""

    summary: str
    intent: MailIntent
    urgency: MailUrgency
    reply_required: bool
    key_points: tuple[str, ...] = ()
    action_items: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "summary", _require_text("summary", self.summary, max_length=8_000)
        )
        if not isinstance(self.intent, MailIntent):
            raise TypeError("intent must be a MailIntent")
        if not isinstance(self.urgency, MailUrgency):
            raise TypeError("urgency must be a MailUrgency")
        if type(self.reply_required) is not bool:
            raise TypeError("reply_required must be a bool")
        object.__setattr__(
            self,
            "key_points",
            _normalize_text_items("key_points", self.key_points, max_items=30),
        )
        object.__setattr__(
            self,
            "action_items",
            _normalize_text_items("action_items", self.action_items, max_items=30),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EmailAnalysis:
    """A structured analysis tied to one accessible, archived email."""

    analysis_id: str
    email_id: int
    account_id: int
    summary: str
    intent: MailIntent
    urgency: MailUrgency
    reply_required: bool
    analyzed_at: datetime
    key_points: tuple[str, ...] = ()
    action_items: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "analysis_id", _require_identifier("analysis_id", self.analysis_id)
        )
        _require_positive_int("email_id", self.email_id)
        _require_positive_int("account_id", self.account_id)
        object.__setattr__(
            self, "summary", _require_text("summary", self.summary, max_length=8_000)
        )
        if not isinstance(self.intent, MailIntent):
            raise TypeError("intent must be a MailIntent")
        if not isinstance(self.urgency, MailUrgency):
            raise TypeError("urgency must be a MailUrgency")
        if type(self.reply_required) is not bool:
            raise TypeError("reply_required must be a bool")
        _require_aware_datetime("analyzed_at", self.analyzed_at)
        object.__setattr__(
            self,
            "key_points",
            _normalize_text_items("key_points", self.key_points, max_items=30),
        )
        object.__setattr__(
            self,
            "action_items",
            _normalize_text_items("action_items", self.action_items, max_items=30),
        )

    @classmethod
    def from_proposal(
        cls,
        *,
        analysis_id: str,
        email_id: int,
        account_id: int,
        proposal: EmailAnalysisProposal,
        analyzed_at: datetime,
    ) -> EmailAnalysis:
        """Attach trusted mail identity and timestamp to a model proposal."""

        if not isinstance(proposal, EmailAnalysisProposal):
            raise TypeError("proposal must be an EmailAnalysisProposal")
        return cls(
            analysis_id=analysis_id,
            email_id=email_id,
            account_id=account_id,
            summary=proposal.summary,
            intent=proposal.intent,
            urgency=proposal.urgency,
            reply_required=proposal.reply_required,
            analyzed_at=analyzed_at,
            key_points=proposal.key_points,
            action_items=proposal.action_items,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplyDraftProposal:
    """Model proposal for reply content; it has no approval authority."""

    recipients: tuple[str, ...]
    subject: str
    body_text: str

    def __post_init__(self) -> None:
        recipients = _normalize_text_items("recipients", self.recipients, max_items=20)
        if not recipients:
            raise ValueError("recipients must not be empty")
        normalized_recipients = tuple(recipient.casefold() for recipient in recipients)
        if len(normalized_recipients) != len(set(normalized_recipients)):
            raise ValueError("recipients must not contain duplicates")
        object.__setattr__(self, "recipients", recipients)
        object.__setattr__(
            self, "subject", _require_text("subject", self.subject, max_length=1_000)
        )
        object.__setattr__(
            self, "body_text", _require_text("body_text", self.body_text, max_length=100_000)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplyDraft:
    """One immutable, versioned reply draft and its current review state."""

    draft_id: str
    email_id: int
    account_id: int
    analysis_id: str
    version: int
    status: DraftStatus
    recipients: tuple[str, ...]
    subject: str
    body_text: str
    created_at: datetime
    updated_at: datetime
    created_by: str
    updated_by: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_comment: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "draft_id", _require_identifier("draft_id", self.draft_id))
        _require_positive_int("email_id", self.email_id)
        _require_positive_int("account_id", self.account_id)
        object.__setattr__(
            self, "analysis_id", _require_identifier("analysis_id", self.analysis_id)
        )
        _require_positive_int("version", self.version)
        if not isinstance(self.status, DraftStatus):
            raise TypeError("status must be a DraftStatus")

        recipients = _normalize_text_items("recipients", self.recipients, max_items=20)
        if not recipients:
            raise ValueError("recipients must not be empty")
        normalized_recipients = tuple(recipient.casefold() for recipient in recipients)
        if len(normalized_recipients) != len(set(normalized_recipients)):
            raise ValueError("recipients must not contain duplicates")
        object.__setattr__(self, "recipients", recipients)
        object.__setattr__(
            self, "subject", _require_text("subject", self.subject, max_length=1_000)
        )
        object.__setattr__(
            self, "body_text", _require_text("body_text", self.body_text, max_length=100_000)
        )

        created_at = _require_aware_datetime("created_at", self.created_at)
        updated_at = _require_aware_datetime("updated_at", self.updated_at)
        if updated_at < created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        object.__setattr__(self, "created_by", _require_identifier("created_by", self.created_by))
        object.__setattr__(self, "updated_by", _require_identifier("updated_by", self.updated_by))
        review_comment = _normalize_optional_text(
            "review_comment", self.review_comment, max_length=4_000
        )
        object.__setattr__(self, "review_comment", review_comment)

        has_review = self.reviewed_by is not None or self.reviewed_at is not None
        if (self.reviewed_by is None) != (self.reviewed_at is None):
            raise ValueError("reviewer identity and time must be provided together")
        is_reviewed_status = self.status in {DraftStatus.APPROVED, DraftStatus.REJECTED}
        if is_reviewed_status and not has_review:
            raise ValueError("approved or rejected drafts require reviewer metadata")
        if not is_reviewed_status and (has_review or review_comment is not None):
            raise ValueError("only approved or rejected drafts may contain review metadata")
        if self.reviewed_by is not None:
            object.__setattr__(
                self, "reviewed_by", _require_identifier("reviewed_by", self.reviewed_by)
            )
        if self.reviewed_at is not None:
            reviewed_at = _require_aware_datetime("reviewed_at", self.reviewed_at)
            if reviewed_at < created_at:
                raise ValueError("reviewed_at must not be earlier than created_at")

    @classmethod
    def from_proposal(
        cls,
        *,
        draft_id: str,
        email_id: int,
        account_id: int,
        analysis_id: str,
        proposal: ReplyDraftProposal,
        created_at: datetime,
        created_by: str,
    ) -> ReplyDraft:
        """Create the first, unapproved version from a model proposal."""

        if not isinstance(proposal, ReplyDraftProposal):
            raise TypeError("proposal must be a ReplyDraftProposal")
        return cls(
            draft_id=draft_id,
            email_id=email_id,
            account_id=account_id,
            analysis_id=analysis_id,
            version=1,
            status=DraftStatus.DRAFT,
            recipients=proposal.recipients,
            subject=proposal.subject,
            body_text=proposal.body_text,
            created_at=created_at,
            updated_at=created_at,
            created_by=created_by,
            updated_by=created_by,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftTransition:
    """Append-only audit event that accompanies one draft version write."""

    draft_id: str
    from_version: int | None
    to_version: int
    from_status: DraftStatus | None
    to_status: DraftStatus
    kind: DraftTransitionKind
    actor_id: str
    occurred_at: datetime
    comment: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "draft_id", _require_identifier("draft_id", self.draft_id))
        if self.from_version is not None:
            _require_positive_int("from_version", self.from_version)
            if self.to_version != self.from_version + 1:
                raise ValueError("to_version must be exactly one greater than from_version")
        _require_positive_int("to_version", self.to_version)
        if not isinstance(self.to_status, DraftStatus):
            raise TypeError("to_status must be a DraftStatus")
        if self.from_status is not None and not isinstance(self.from_status, DraftStatus):
            raise TypeError("from_status must be a DraftStatus or None")
        if not isinstance(self.kind, DraftTransitionKind):
            raise TypeError("kind must be a DraftTransitionKind")
        if self.kind is not DraftTransitionKind.CREATED and self.from_version is None:
            raise ValueError("non-created transitions require from_version")
        object.__setattr__(self, "actor_id", _require_identifier("actor_id", self.actor_id))
        _require_aware_datetime("occurred_at", self.occurred_at)
        object.__setattr__(
            self,
            "comment",
            _normalize_optional_text("comment", self.comment, max_length=4_000),
        )
        self._validate_shape()

    def _validate_shape(self) -> None:
        expected: dict[
            DraftTransitionKind,
            tuple[DraftStatus | None, DraftStatus],
        ] = {
            DraftTransitionKind.CREATED: (None, DraftStatus.DRAFT),
            DraftTransitionKind.REVISED: (DraftStatus.DRAFT, DraftStatus.DRAFT),
            DraftTransitionKind.SUBMITTED_FOR_REVIEW: (
                DraftStatus.DRAFT,
                DraftStatus.PENDING_REVIEW,
            ),
            DraftTransitionKind.APPROVED: (DraftStatus.PENDING_REVIEW, DraftStatus.APPROVED),
            DraftTransitionKind.REJECTED: (DraftStatus.PENDING_REVIEW, DraftStatus.REJECTED),
            DraftTransitionKind.WITHDRAWN: (DraftStatus.PENDING_REVIEW, DraftStatus.DRAFT),
        }
        expected_from, expected_to = expected[self.kind]
        if self.kind is DraftTransitionKind.REVISED:
            valid_from = {DraftStatus.DRAFT, DraftStatus.REJECTED}
            if self.from_status not in valid_from or self.to_status is not expected_to:
                raise ValueError("revised transitions must move draft or rejected to draft")
            return
        if self.from_status is not expected_from or self.to_status is not expected_to:
            raise ValueError("transition status values do not match its kind")
        if self.kind is DraftTransitionKind.CREATED and (
            self.from_version is not None or self.to_version != 1
        ):
            raise ValueError("created transitions must create version 1")


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftRevisionRequest:
    """Human instruction used to request a new, unapproved draft revision."""

    instruction: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instruction",
            _require_text("instruction", self.instruction, max_length=4_000),
        )
