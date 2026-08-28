"""Typed, read-only tool for searching archived mail."""

from __future__ import annotations

from app.schemas.mail_query import MailSearchCriteria
from app.schemas.tools import ToolErrorCode
from app.services.mail_query import MailAccessDeniedError, MailQueryService
from app.tools.base import ToolContext, ToolExecutionError, TypedTool
from app.tools.schemas import MailSearchItemResult, SearchMailArgs, SearchMailResult


class SearchMailTool(TypedTool[SearchMailArgs, SearchMailResult]):
    """Search stored mail while preserving the trusted account scope."""

    name = "search_mail"
    description = (
        "Search archived mail by text and/or sender. Returns compact metadata and text snippets "
        "only from accounts the current caller may access."
    )
    args_model = SearchMailArgs
    result_model = SearchMailResult

    def __init__(self, mail_query: MailQueryService) -> None:
        self._mail_query = mail_query

    async def execute(self, arguments: SearchMailArgs, context: ToolContext) -> SearchMailResult:
        if (
            arguments.account_id is not None
            and arguments.account_id not in context.allowed_account_ids
        ):
            raise ToolExecutionError(
                ToolErrorCode.FORBIDDEN,
                "The requested account is not available for this request.",
            )

        try:
            items = await self._mail_query.search(
                MailSearchCriteria(
                    text=arguments.query,
                    sender=arguments.sender,
                    account_id=arguments.account_id,
                    limit=arguments.limit,
                ),
                allowed_account_ids=context.allowed_account_ids,
            )
        except MailAccessDeniedError as exc:
            raise ToolExecutionError(
                ToolErrorCode.FORBIDDEN,
                "The requested account is not available for this request.",
            ) from exc

        results = [
            MailSearchItemResult(
                email_id=item.email_id,
                account_id=item.account_id,
                subject=item.subject,
                sender=item.sender,
                sent_at=item.sent_at,
                snippet=item.snippet,
                fetched_at=item.fetched_at,
            )
            for item in items
        ]
        return SearchMailResult(items=results, returned_count=len(results))
