"""Pydantic input and output contracts owned by concrete mail tools."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictToolModel(BaseModel):
    """Base class for tool contracts with no silent extra fields."""

    model_config = ConfigDict(extra="forbid")


class ToolInputModel(StrictToolModel):
    """Tool inputs normalize harmless leading and trailing whitespace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchMailArgs(ToolInputModel):
    """Arguments accepted by the read-only ``search_mail`` tool."""

    query: str | None = Field(default=None, min_length=1, max_length=200)
    sender: str | None = Field(default=None, min_length=1, max_length=320)
    account_id: int | None = Field(default=None, gt=0)
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def _requires_a_filter(self) -> Self:
        if self.query is None and self.sender is None:
            raise ValueError("at least one of query or sender is required")
        return self


class GetEmailContextArgs(ToolInputModel):
    """Arguments accepted by the read-only ``get_email_context`` tool."""

    email_id: int = Field(gt=0)
    max_body_chars: int = Field(default=4_000, ge=1, le=12_000)


class MailSearchItemResult(StrictToolModel):
    """One compact search result suitable for a model observation."""

    email_id: int = Field(gt=0)
    account_id: int = Field(gt=0)
    subject: str
    sender: str | None = None
    sent_at: datetime | None = None
    snippet: str | None = None
    fetched_at: datetime | None = None
    source: Literal["email_store"] = "email_store"


class SearchMailResult(StrictToolModel):
    """Successful ``search_mail`` output."""

    items: list[MailSearchItemResult]
    returned_count: int = Field(ge=0)


class EmailContextResult(StrictToolModel):
    """Successful, bounded ``get_email_context`` output."""

    email_id: int = Field(gt=0)
    account_id: int = Field(gt=0)
    uid: int = Field(ge=0)
    message_id: str | None = None
    subject: str
    sender: str | None = None
    recipients: list[str]
    sent_at: datetime | None = None
    text_body: str
    body_truncated: bool
    fetched_at: datetime | None = None
    source: Literal["email_store"] = "email_store"
