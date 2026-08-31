"""Ports required by the archived-mail analysis and draft-review use cases."""

from __future__ import annotations

from typing import Protocol

from app.schemas.mail_query import MailContext
from app.schemas.mail_workflow import (
    DraftTransition,
    EmailAnalysis,
    EmailAnalysisProposal,
    ReplyDraft,
    ReplyDraftProposal,
)


class MailWorkflowError(Exception):
    """Base class for predictable mail-analysis and draft-workflow failures."""


class DraftVersionConflictError(MailWorkflowError, RuntimeError):
    """Raised when an append loses the compare-and-swap race for a draft version."""


class ArchivedMailContextReader(Protocol):
    """Read one archived email while enforcing the trusted account scope."""

    async def get_context(
        self,
        email_id: int,
        *,
        allowed_account_ids: frozenset[int],
    ) -> MailContext | None:
        """Return an accessible context, or ``None`` without disclosing ownership."""


class MailAnalyzer(Protocol):
    """Produce a bounded, typed analysis proposal from archived mail context."""

    async def analyze(self, mail: MailContext) -> EmailAnalysisProposal:
        """Return a proposal; it has no trusted identity or persistence authority."""


class ReplyDraftGenerator(Protocol):
    """Produce unapproved reply content from an email and its typed analysis."""

    async def generate(
        self,
        mail: MailContext,
        analysis: EmailAnalysis,
        *,
        previous_draft: ReplyDraft | None,
        instruction: str | None,
    ) -> ReplyDraftProposal:
        """Return draft content only; this port cannot approve or send a draft."""


class MailAnalysisStore(Protocol):
    """Persist and retrieve analysis records independently of a concrete database."""

    async def save(self, analysis: EmailAnalysis) -> EmailAnalysis:
        """Durably save one analysis record and return the stored projection."""

    async def get(
        self,
        analysis_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> EmailAnalysis | None:
        """Return an accessible analysis, or ``None`` when unavailable."""


class ReplyDraftStore(Protocol):
    """Persist immutable draft versions and their append-only review audit trail.

    ``create`` and ``append_revision`` must write the draft version and its
    ``DraftTransition`` atomically.  ``append_revision`` must compare the
    current version with ``expected_version`` and raise
    :class:`DraftVersionConflictError` when they differ.
    """

    async def create(self, draft: ReplyDraft, transition: DraftTransition) -> ReplyDraft:
        """Create the initial version and its ``created`` audit event atomically."""

    async def get_current(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> ReplyDraft | None:
        """Return the latest accessible version, or ``None`` when unavailable."""

    async def list_versions(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[ReplyDraft]:
        """Return accessible versions in ascending version order."""

    async def append_revision(
        self,
        draft: ReplyDraft,
        transition: DraftTransition,
        *,
        expected_version: int,
    ) -> ReplyDraft:
        """Append one version/audit event using optimistic concurrency control."""
