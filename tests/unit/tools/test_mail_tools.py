"""Tool-boundary tests using a fake deterministic mail-query service."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.schemas import MailContext, MailSearchCriteria, MailSearchItem
from app.schemas.tools import ToolDefinition, ToolErrorCode, ToolInvocationResult
from app.tools import (
    GetEmailContextTool,
    SearchMailTool,
    ToolContext,
    ToolRegistry,
    build_default_tool_registry,
)


class FakeMailQueryService:
    def __init__(self) -> None:
        self.search_calls: list[tuple[MailSearchCriteria, frozenset[int]]] = []
        self.context_calls: list[tuple[int, frozenset[int]]] = []
        self.items = [
            MailSearchItem(
                email_id=5,
                account_id=1,
                subject="Quote request",
                sender="seller@example.com",
                sent_at=datetime(2026, 8, 20, tzinfo=UTC),
                snippet="The requested quote is attached.",
                fetched_at=datetime(2026, 8, 21, tzinfo=UTC),
            )
        ]
        self.context: MailContext | None = MailContext(
            email_id=5,
            account_id=1,
            uid=88,
            message_id="<mail-5@example.com>",
            subject="Quote request",
            sender="seller@example.com",
            recipients=("team@example.com",),
            sent_at=datetime(2026, 8, 20, tzinfo=UTC),
            text_body="abcdefghij",
            fetched_at=datetime(2026, 8, 21, tzinfo=UTC),
        )

    async def search(
        self,
        criteria: MailSearchCriteria,
        *,
        allowed_account_ids: frozenset[int],
    ) -> list[MailSearchItem]:
        self.search_calls.append((criteria, allowed_account_ids))
        return self.items

    async def get_context(
        self,
        email_id: int,
        *,
        allowed_account_ids: frozenset[int],
    ) -> MailContext | None:
        self.context_calls.append((email_id, allowed_account_ids))
        return self.context


async def test_registry_exposes_json_schema_and_executes_search() -> None:
    service = FakeMailQueryService()
    registry = build_default_tool_registry(service)
    context = ToolContext(allowed_account_ids=frozenset({1}))

    result = await registry.invoke(
        "search_mail",
        {"query": "quote", "limit": 3},
        context,
    )

    definitions = {definition.name: definition for definition in registry.definitions}
    assert set(definitions) == {"get_email_context", "search_mail"}
    assert "query" in definitions["search_mail"].parameters["properties"]
    assert result.ok is True
    assert result.result is not None
    assert result.result["returned_count"] == 1
    assert result.result["items"][0]["email_id"] == 5
    assert service.search_calls == [
        (MailSearchCriteria(text="quote", sender=None, account_id=None, limit=3), frozenset({1}))
    ]


async def test_search_rejects_out_of_scope_account_before_service_call() -> None:
    service = FakeMailQueryService()
    registry = ToolRegistry((SearchMailTool(service),))

    result = await registry.invoke(
        "search_mail",
        {"query": "quote", "account_id": 2},
        ToolContext(allowed_account_ids=frozenset({1})),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ToolErrorCode.FORBIDDEN
    assert service.search_calls == []


async def test_registry_returns_stable_errors_for_invalid_unknown_and_missing_calls() -> None:
    service = FakeMailQueryService()
    registry = ToolRegistry((SearchMailTool(service), GetEmailContextTool(service)))
    context = ToolContext(allowed_account_ids=frozenset({1}))

    invalid = await registry.invoke("search_mail", {"limit": 1}, context)
    unknown = await registry.invoke("delete_mail", {}, context)
    service.context = None
    missing = await registry.invoke("get_email_context", {"email_id": 5}, context)

    assert invalid.error is not None and invalid.error.code is ToolErrorCode.INVALID_ARGUMENT
    assert unknown.error is not None and unknown.error.code is ToolErrorCode.UNKNOWN_TOOL
    assert missing.error is not None and missing.error.code is ToolErrorCode.NOT_FOUND


async def test_context_tool_bounds_the_returned_body() -> None:
    service = FakeMailQueryService()
    registry = ToolRegistry((GetEmailContextTool(service),))

    result = await registry.invoke(
        "get_email_context",
        {"email_id": 5, "max_body_chars": 4},
        ToolContext(allowed_account_ids=frozenset({1})),
    )

    assert result.ok is True
    assert result.result is not None
    assert result.result["text_body"] == "abcd"
    assert result.result["body_truncated"] is True


class SlowTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="slow_tool",
            description="Test-only slow tool.",
            parameters={"type": "object"},
            result_schema={"type": "object"},
        )

    async def invoke(self, raw_arguments, context) -> ToolInvocationResult:
        await asyncio.sleep(0.05)
        return ToolInvocationResult(tool_name="slow_tool", ok=True, result={})


async def test_registry_returns_timeout_observation() -> None:
    registry = ToolRegistry((SlowTool(),), timeout_seconds=0.001)

    result = await registry.invoke("slow_tool", {}, ToolContext(allowed_account_ids=frozenset()))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ToolErrorCode.TIMEOUT
