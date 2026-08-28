"""Public contracts for one bounded, read-only agent run."""

from __future__ import annotations

from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.llm import LLMMessage, LLMMessageRole
from app.schemas.tools import ToolErrorCode


class AgentTerminationReason(StrEnum):
    """Stable terminal states exposed by the minimal agent runtime."""

    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    MODEL_TIMEOUT = "model_timeout"
    TOOL_CALL_LIMIT = "tool_call_limit"


class AgentRunRequest(BaseModel):
    """Trusted input for one graph invocation.

    ``allowed_account_ids`` is supplied by server-side authorization code.  It
    is intentionally kept out of graph state and handed to the tool node only
    through LangGraph's runtime context.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    messages: list[LLMMessage] = Field(min_length=1)
    allowed_account_ids: tuple[int, ...] = ()
    max_steps: int = Field(default=4, ge=1, le=20)
    max_tool_calls_per_turn: int = Field(default=8, ge=1, le=20)
    model_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    run_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)

    @field_validator("allowed_account_ids")
    @classmethod
    def _requires_positive_unique_account_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(type(account_id) is not int or account_id <= 0 for account_id in value):
            raise ValueError("allowed_account_ids must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("allowed_account_ids must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _accepts_only_initial_messages(self) -> AgentRunRequest:
        if any(
            message.role not in {LLMMessageRole.SYSTEM, LLMMessageRole.USER}
            for message in self.messages
        ):
            raise ValueError("a new agent run accepts only system and user messages")
        return self


class AgentToolEvent(BaseModel):
    """Safe audit record for one tool dispatch, without raw arguments or mail data."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    ok: bool
    duration_ms: int = Field(ge=0)
    error_code: ToolErrorCode | None = None

    @model_validator(mode="after")
    def _matches_terminal_tool_status(self) -> AgentToolEvent:
        if self.ok and self.error_code is not None:
            raise ValueError("successful tool events cannot contain an error code")
        if not self.ok and self.error_code is None:
            raise ValueError("failed tool events require an error code")
        return self


class AgentRunResult(BaseModel):
    """Safe result returned after the graph reaches a terminal state."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    answer: str | None = None
    model_turns: int = Field(ge=0)
    termination_reason: AgentTerminationReason
    tool_events: tuple[AgentToolEvent, ...] = ()
