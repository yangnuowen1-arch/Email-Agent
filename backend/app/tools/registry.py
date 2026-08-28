"""Registry and bounded dispatcher for model-visible tools."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import Any

from app.schemas.tools import ToolDefinition, ToolErrorCode, ToolInvocationResult
from app.services.mail_query import MailQueryService
from app.tools.base import RegisteredTool, ToolContext
from app.tools.get_email_context import GetEmailContextTool
from app.tools.search_mail import SearchMailTool


class ToolRegistry:
    """Expose a stable set of tools and apply a per-invocation timeout."""

    def __init__(self, tools: Iterable[RegisteredTool], *, timeout_seconds: float = 10.0) -> None:
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        registered: dict[str, RegisteredTool] = {}
        for tool in tools:
            name = tool.definition.name
            if name in registered:
                raise ValueError(f"duplicate tool name: {name}")
            registered[name] = tool

        self._tools = registered
        self._timeout_seconds = float(timeout_seconds)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return tool metadata in registration order for an LLM gateway."""

        return tuple(tool.definition for tool in self._tools.values())

    async def invoke(
        self,
        tool_name: str,
        raw_arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolInvocationResult:
        """Dispatch one validated tool call with a bounded execution time."""

        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolInvocationResult.failure(
                tool_name=tool_name,
                code=ToolErrorCode.UNKNOWN_TOOL,
                message="This tool is not available for the current request.",
            )

        try:
            return await asyncio.wait_for(
                tool.invoke(raw_arguments, context),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return ToolInvocationResult.failure(
                tool_name=tool_name,
                code=ToolErrorCode.TIMEOUT,
                message="The tool did not finish before its timeout.",
                retryable=True,
            )
        except Exception:  # noqa: BLE001 - custom registered tools may fail unexpectedly
            return ToolInvocationResult.failure(
                tool_name=tool_name,
                code=ToolErrorCode.INTERNAL_ERROR,
                message="The tool could not complete the request.",
                retryable=True,
            )


def build_default_tool_registry(
    mail_query: MailQueryService,
    *,
    timeout_seconds: float = 10.0,
) -> ToolRegistry:
    """Build the model-visible default tool set in one discoverable location.

    New default tools are added here rather than in the composition root.  The
    container only supplies already-wired service dependencies.
    """

    return ToolRegistry(
        (
            SearchMailTool(mail_query),
            GetEmailContextTool(mail_query),
        ),
        timeout_seconds=timeout_seconds,
    )
