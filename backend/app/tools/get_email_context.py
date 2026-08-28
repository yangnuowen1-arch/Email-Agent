"""Typed, read-only tool for retrieving one archived mail's context."""

from __future__ import annotations

from app.schemas.tools import ToolErrorCode
from app.services.mail_query import MailQueryService
from app.tools.base import ToolContext, ToolExecutionError, TypedTool
from app.tools.schemas import EmailContextResult, GetEmailContextArgs


class GetEmailContextTool(TypedTool[GetEmailContextArgs, EmailContextResult]):
    """Return bounded plain-text context for one accessible mail message."""

    name = "get_email_context"
    description = (
        "Retrieve bounded plain-text context for one archived email by ID. "
        "The email must belong to an account the current caller may access."
    )
    args_model = GetEmailContextArgs
    result_model = EmailContextResult

    def __init__(self, mail_query: MailQueryService) -> None:
        self._mail_query = mail_query

    async def execute(
        self,
        arguments: GetEmailContextArgs,
        context: ToolContext,
    ) -> EmailContextResult:
        mail = await self._mail_query.get_context(
            arguments.email_id,
            allowed_account_ids=context.allowed_account_ids,
        )
        if mail is None:
            raise ToolExecutionError(
                ToolErrorCode.NOT_FOUND,
                "No accessible email was found for this ID.",
            )

        text_body = mail.text_body or ""
        body_truncated = len(text_body) > arguments.max_body_chars
        if body_truncated:
            text_body = text_body[: arguments.max_body_chars]

        return EmailContextResult(
            email_id=mail.email_id,
            account_id=mail.account_id,
            uid=mail.uid,
            message_id=mail.message_id,
            subject=mail.subject,
            sender=mail.sender,
            recipients=list(mail.recipients),
            sent_at=mail.sent_at,
            text_body=text_body,
            body_truncated=body_truncated,
            fetched_at=mail.fetched_at,
        )
