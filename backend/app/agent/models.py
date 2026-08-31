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
    RETRY_EXHAUSTED = "retry_exhausted"
    NON_RETRYABLE_ERROR = "non_retryable_error"


class AgentNodeName(StrEnum):
    """Graph node names exposed in safe execution telemetry."""

    MODEL = "model"
    TOOLS = "tools"


class AgentNodeErrorKind(StrEnum):
    """Stable, non-sensitive failure categories for graph-node telemetry."""

    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    NON_RETRYABLE = "non_retryable"


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
    node_retry_max_attempts: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Maximum attempts per transient graph-node failure, including the first.",
    )
    node_retry_initial_interval_seconds: float = Field(
        default=0.1,
        ge=0,
        le=10,
        description="Delay before the first graph-node retry; later delays use bounded backoff.",
    )
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


class AgentNodeEvent(BaseModel):
    """Safe audit event emitted whenever a graph node attempt fails.

    Raw provider exception text is deliberately omitted because it can contain
    request metadata or credentials.  ``attempt`` is local to the current
    node invocation, so a later successful model turn starts again at one.
    """

    model_config = ConfigDict(extra="forbid")

    node: AgentNodeName
    attempt: int = Field(ge=1)
    error_kind: AgentNodeErrorKind
    retryable: bool
    will_retry: bool

    @model_validator(mode="after")
    def _matches_retry_decision(self) -> AgentNodeEvent:
        if self.will_retry and not self.retryable:
            raise ValueError("only retryable node failures can be retried")
        if self.error_kind is AgentNodeErrorKind.NON_RETRYABLE and self.retryable:
            raise ValueError("non-retryable node failures cannot be retryable")
        return self


class AgentRunResult(BaseModel):
    """Safe result returned after the graph reaches a terminal state."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    answer: str | None = None
    model_turns: int = Field(ge=0)
    termination_reason: AgentTerminationReason
    tool_events: tuple[AgentToolEvent, ...] = ()
    node_events: tuple[AgentNodeEvent, ...] = ()
