"""Pydantic contracts exposed at the model-tool boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolErrorCode(StrEnum):
    """Stable error categories an agent can handle without parsing prose."""

    FORBIDDEN = "forbidden"
    INTERNAL_ERROR = "internal_error"
    INVALID_ARGUMENT = "invalid_argument"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    UNKNOWN_TOOL = "unknown_tool"


class ToolError(BaseModel):
    """A safe, structured tool failure observation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: ToolErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False


class ToolInvocationResult(BaseModel):
    """Serialized result returned from a registry dispatch."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=64)
    ok: bool
    result: dict[str, Any] | None = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def _has_one_terminal_shape(self) -> Self:
        if self.ok and (self.result is None or self.error is not None):
            raise ValueError("successful tool results require result and no error")
        if not self.ok and (self.result is not None or self.error is None):
            raise ValueError("failed tool results require error and no result")
        return self

    @classmethod
    def failure(
        cls,
        *,
        tool_name: str,
        code: ToolErrorCode,
        message: str,
        retryable: bool = False,
    ) -> Self:
        """Build a validated error observation."""

        return cls(
            tool_name=tool_name,
            ok=False,
            error=ToolError(code=code, message=message, retryable=retryable),
        )


class ToolDefinition(BaseModel):
    """Provider-neutral metadata a gateway can expose to a model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=1_000)
    parameters: dict[str, Any]
    result_schema: dict[str, Any]
