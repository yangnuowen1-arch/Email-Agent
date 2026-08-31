"""Typed node failures used to drive bounded LangGraph retries."""

from __future__ import annotations

from app.agent.models import AgentNodeErrorKind, AgentNodeName, AgentTerminationReason


class AgentNodeError(Exception):
    """Base class for a classified graph-node failure.

    The public exception text intentionally contains only a stable category;
    provider exception details stay in the chained exception for server-side
    diagnostics and never enter an ``AgentRunResult``.
    """

    retryable: bool = False

    def __init__(
        self,
        *,
        node: AgentNodeName,
        error_kind: AgentNodeErrorKind,
        termination_reason: AgentTerminationReason,
    ) -> None:
        self.node = node
        self.error_kind = error_kind
        self.termination_reason = termination_reason
        super().__init__(f"{node.value} node failed: {error_kind.value}")


class TransientAgentNodeError(AgentNodeError):
    """A classified node failure eligible for the graph's retry policy."""

    retryable = True


class NonRetryableAgentNodeError(AgentNodeError):
    """A classified node failure that must terminate without another attempt."""
