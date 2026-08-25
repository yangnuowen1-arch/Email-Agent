"""Command-line entry point for the starter agent."""

from __future__ import annotations

import asyncio

from .agent import EmailAgent
from .config import get_settings


def main() -> None:
    """Run the local, network-free demonstration flow."""
    agent = EmailAgent(get_settings())
    response = asyncio.run(agent.respond("Draft a concise welcome email."))
    print(response.text)


if __name__ == "__main__":
    main()
