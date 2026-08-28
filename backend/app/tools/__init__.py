"""Typed, model-visible tool adapters available to a future agent runtime."""

from .base import ToolContext, ToolExecutionError, TypedTool
from .get_email_context import GetEmailContextTool
from .registry import ToolRegistry, build_default_tool_registry
from .search_mail import SearchMailTool

__all__ = [
    "GetEmailContextTool",
    "SearchMailTool",
    "ToolContext",
    "ToolExecutionError",
    "ToolRegistry",
    "TypedTool",
    "build_default_tool_registry",
]
