"""Basic checks that the starter package can be imported and exercised."""

from __future__ import annotations

import asyncio

from email_agent import __version__
from email_agent.agent import EmailAgent
from email_agent.config import Settings
from email_agent.tools import search


def test_package_version_is_exposed() -> None:
    assert __version__ == "0.1.0"


def test_agent_runs_with_local_client() -> None:
    response = asyncio.run(EmailAgent(Settings()).respond("Say hello."))
    assert "Say hello." in response.text


def test_search_placeholder_is_safe_without_provider() -> None:
    assert asyncio.run(search("email etiquette")) == []
