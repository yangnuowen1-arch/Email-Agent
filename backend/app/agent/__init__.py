"""Bounded LangGraph runtime for the read-only mail agent."""

from app.agent.graph import EmailAgent
from app.agent.models import (
    AgentRunRequest,
    AgentRunResult,
    AgentTerminationReason,
    AgentToolEvent,
)

__all__ = [
    "AgentRunRequest",
    "AgentRunResult",
    "AgentTerminationReason",
    "AgentToolEvent",
    "EmailAgent",
]
