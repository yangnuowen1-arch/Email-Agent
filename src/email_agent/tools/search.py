"""Search-tool placeholder with a stable interface for future integrations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


async def search(query: str) -> list[SearchResult]:
    """Return no results until a search provider is configured."""
    if not query.strip():
        return []
    return []
