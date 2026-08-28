"""Shared execution boundary for model-visible typed tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.schemas.tools import ToolDefinition, ToolErrorCode, ToolInvocationResult

ArgsT = TypeVar("ArgsT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Trusted server-side scope supplied for a single tool invocation."""

    allowed_account_ids: frozenset[int]

    def __post_init__(self) -> None:
        account_ids = frozenset(self.allowed_account_ids)
        if any(type(account_id) is not int or account_id <= 0 for account_id in account_ids):
            raise ValueError("allowed_account_ids must contain positive integers")
        object.__setattr__(self, "allowed_account_ids", account_ids)


class ToolExecutionError(Exception):
    """Expected execution failure that can safely become a model observation."""

    def __init__(
        self,
        code: ToolErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class RegisteredTool(Protocol):
    """The small surface required by :class:`ToolRegistry`."""

    @property
    def definition(self) -> ToolDefinition:
        """Return provider-neutral metadata for this tool."""

    async def invoke(
        self,
        raw_arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolInvocationResult:
        """Validate and execute one tool call."""


class TypedTool(ABC, Generic[ArgsT, ResultT]):
    """Validate model-supplied arguments before invoking deterministic logic."""

    name: str
    description: str
    args_model: type[ArgsT]
    result_model: type[ResultT]

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
            result_schema=self.result_model.model_json_schema(),
        )

    async def invoke(
        self,
        raw_arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolInvocationResult:
        """Return success or a safe structured observation; never leak raw errors."""

        try:
            arguments = self.args_model.model_validate(raw_arguments)
        except ValidationError:
            return ToolInvocationResult.failure(
                tool_name=self.name,
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="Tool arguments did not match the required schema.",
            )

        try:
            result = await self.execute(arguments, context)
        except ToolExecutionError as exc:
            return ToolInvocationResult.failure(
                tool_name=self.name,
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
            )
        except Exception:  # noqa: BLE001 - tool callers receive a stable observation
            return ToolInvocationResult.failure(
                tool_name=self.name,
                code=ToolErrorCode.INTERNAL_ERROR,
                message="The tool could not complete the request.",
                retryable=True,
            )

        return ToolInvocationResult(
            tool_name=self.name,
            ok=True,
            result=result.model_dump(mode="json"),
        )

    @abstractmethod
    async def execute(self, arguments: ArgsT, context: ToolContext) -> ResultT:
        """Run the tool's deterministic use case after schema validation."""
