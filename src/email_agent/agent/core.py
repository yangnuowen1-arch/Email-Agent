"""Framework-independent core orchestration for the email agent."""

from __future__ import annotations

from ..config import Settings
from ..llm.client import EchoLLMClient, LLMClient, LLMResponse
from .prompts import SYSTEM_PROMPT


class EmailAgent:
    """Minimal agent facade; replace the echo client with a real provider later."""

    def __init__(self, settings: Settings, client: LLMClient | None = None) -> None:
        self.settings = settings
        self.client = client or EchoLLMClient()

    async def respond(self, task: str) -> LLMResponse:
        """Build a prompt and delegate completion to the configured client."""
        prompt = f"{SYSTEM_PROMPT}\n\nTask: {task.strip()}"
        return await self.client.complete(prompt)
