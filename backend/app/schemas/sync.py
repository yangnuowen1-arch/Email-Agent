"""Inbound-mail sync contracts shared by services, adapters, and the CLI.

These are pure data structures: they do not know about IMAP, SQLAlchemy, or a
particular command-line framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.email import RawEmail


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncRequest:
    """The caller's requested sync mode."""

    full: bool = False
    limit: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MailboxReadResult:
    """Raw mail returned by an inbound adapter plus UIDs it could not provide."""

    messages: tuple[RawEmail, ...] = ()
    failed_uids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PersistResult:
    """The storage adapter's idempotent-write outcome."""

    inserted: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountSyncResult:
    """One mailbox's observable sync outcome."""

    account_id: int
    name: str
    inserted: int = 0
    skipped: int = 0
    failed_uids: tuple[int, ...] = ()
    checkpoint_advanced: bool = False
    error: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncReport:
    """Batch report returned to CLI/API callers."""

    results: tuple[AccountSyncResult, ...] = field(default_factory=tuple)
    total_inserted: int = 0
    total_skipped: int = 0
    total_failed: int = 0
    duration_ms: int = 0
