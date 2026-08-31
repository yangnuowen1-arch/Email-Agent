"""Local CLI commands for the human-reviewed mail workflow.

The commands deliberately receive only a *profile name*.  They resolve the
trusted actor, roles, and account scope from ``AppConfig``; accepting any of
those values as command-line options would let a local caller impersonate
another workflow user.

``app.cli.main`` owns process startup and puts a :class:`~app.core.container.Container`
in ``ctx.obj``.  It also owns the container lifecycle so help, parse failures,
and workflow commands release resources through the same root-context hook.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, TypeVar, cast

import typer
from sqlalchemy.exc import SQLAlchemyError

from app.core.container import MailWorkflowServices
from app.core.workflow_profiles import WorkflowPrincipal
from app.llm import LLMGateway, LLMRequest, LLMResponse
from app.llm.errors import NonRetryableLLMError, TransientLLMError
from app.ports.mail_workflow import DraftVersionConflictError, MailWorkflowError
from app.schemas import DraftDecision, EmailAnalysis, ReplyDraft
from app.services.mail_workflow import (
    AnalysisNotFoundError,
    ArchivedEmailNotFoundError,
    DraftStateTransitionError,
    ReplyDraftNotFoundError,
)

app = typer.Typer(
    help="Analyze archived mail and move reply drafts through human review.",
    invoke_without_command=True,
    no_args_is_help=True,
)
# A descriptive alias makes registration from the root CLI self-explanatory,
# while ``app`` preserves the conventional Typer module interface.
workflow_app = app


class _ProfileResolver(Protocol):
    """Configuration contract the command module needs from the main CLI."""

    def resolve_workflow_cli_profile(self, profile_name: str) -> WorkflowPrincipal:
        """Resolve a locally configured, trusted workflow principal."""


class _WorkflowContainer(Protocol):
    """Narrow container contract, kept small so CLI tests can use fakes."""

    config: _ProfileResolver

    def build_gemini_gateway(self) -> LLMGateway:
        """Build the configured model gateway for a workflow command."""

    def build_mail_workflow(self, gateway: LLMGateway) -> MailWorkflowServices:
        """Build the scoped mail-analysis and draft services."""

    async def close_all(self) -> None:
        """Release process resources after a command completes."""


_WorkflowResult = TypeVar("_WorkflowResult", EmailAnalysis, ReplyDraft)
_WorkflowOperation = Callable[[MailWorkflowServices, WorkflowPrincipal], Awaitable[_WorkflowResult]]


class _NoModelGateway:
    """Sentinel passed to services for commands that must never call a model."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        del request
        raise AssertionError("a non-generation workflow command attempted to call the model")


class _CommandFailure(Exception):
    """A stable JSON error response with a process exit code."""

    def __init__(self, *, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


def _profile_option() -> str:
    """Declare the repeated profile selector without exposing principal fields."""

    return typer.Option(
        ...,
        "--profile",
        help="Configured local workflow profile name.",
    )


@app.command()
def analyze(
    ctx: typer.Context,
    profile: str = _profile_option(),
    email_id: int = typer.Option(..., "--email-id", min=1, help="Archived email ID."),
) -> None:
    """Create a structured analysis for one accessible archived email."""

    async def operation(
        workflow: MailWorkflowServices,
        principal: WorkflowPrincipal,
    ) -> EmailAnalysis:
        return await workflow.analysis.analyze(
            email_id,
            allowed_account_ids=principal.allowed_account_ids,
        )

    _run_command(
        ctx,
        profile=profile,
        required_role="author",
        requires_model=True,
        operation=operation,
    )


@app.command("create-draft")
def create_draft(
    ctx: typer.Context,
    profile: str = _profile_option(),
    email_id: int = typer.Option(..., "--email-id", min=1, help="Archived email ID."),
    analysis_id: str = typer.Option(..., "--analysis-id", help="Analysis ID for this email."),
) -> None:
    """Generate the first unapproved reply-draft version from an analysis."""

    async def operation(
        workflow: MailWorkflowServices,
        principal: WorkflowPrincipal,
    ) -> ReplyDraft:
        return await workflow.drafts.create(
            email_id,
            analysis_id,
            actor_id=principal.actor_id,
            allowed_account_ids=principal.allowed_account_ids,
        )

    _run_command(
        ctx,
        profile=profile,
        required_role="author",
        requires_model=True,
        operation=operation,
    )


@app.command()
def show(
    ctx: typer.Context,
    profile: str = _profile_option(),
    draft_id: str = typer.Option(..., "--draft-id", help="Reply draft ID."),
) -> None:
    """Show the latest accessible version of a reply draft."""

    async def operation(
        workflow: MailWorkflowServices,
        principal: WorkflowPrincipal,
    ) -> ReplyDraft:
        return await workflow.drafts.get_current(
            draft_id,
            allowed_account_ids=principal.allowed_account_ids,
        )

    _run_command(
        ctx,
        profile=profile,
        required_role=None,
        requires_model=False,
        operation=operation,
    )


@app.command()
def submit(
    ctx: typer.Context,
    profile: str = _profile_option(),
    draft_id: str = typer.Option(..., "--draft-id", help="Reply draft ID."),
    expected_version: int = typer.Option(
        ...,
        "--expected-version",
        min=1,
        help="Current draft version expected by this transition.",
    ),
) -> None:
    """Move an author draft from ``draft`` to ``pending_review``."""

    async def operation(
        workflow: MailWorkflowServices,
        principal: WorkflowPrincipal,
    ) -> ReplyDraft:
        return await workflow.drafts.decide(
            draft_id,
            DraftDecision.SUBMIT_FOR_REVIEW,
            actor_id=principal.actor_id,
            expected_version=expected_version,
            allowed_account_ids=principal.allowed_account_ids,
        )

    _run_command(
        ctx,
        profile=profile,
        required_role="author",
        requires_model=False,
        operation=operation,
    )


@app.command()
def approve(
    ctx: typer.Context,
    profile: str = _profile_option(),
    draft_id: str = typer.Option(..., "--draft-id", help="Reply draft ID."),
    expected_version: int = typer.Option(
        ...,
        "--expected-version",
        min=1,
        help="Current draft version expected by this transition.",
    ),
    comment: str | None = typer.Option(None, "--comment", help="Optional reviewer comment."),
) -> None:
    """Approve a pending-review draft; this command never sends email."""

    async def operation(
        workflow: MailWorkflowServices,
        principal: WorkflowPrincipal,
    ) -> ReplyDraft:
        return await workflow.drafts.decide(
            draft_id,
            DraftDecision.APPROVE,
            actor_id=principal.actor_id,
            expected_version=expected_version,
            allowed_account_ids=principal.allowed_account_ids,
            comment=comment,
        )

    _run_command(
        ctx,
        profile=profile,
        required_role="reviewer",
        requires_model=False,
        operation=operation,
    )


def _run_command(
    ctx: typer.Context,
    *,
    profile: str,
    required_role: str | None,
    requires_model: bool,
    operation: _WorkflowOperation[_WorkflowResult],
) -> None:
    """Run one use case, serialize its result, and map predictable failures."""

    try:
        result = asyncio.run(
            _execute_command(
                ctx,
                profile=profile,
                required_role=required_role,
                requires_model=requires_model,
                operation=operation,
            )
        )
    except _CommandFailure as exc:
        _emit_error(exc.code, exc.message)
        raise typer.Exit(code=exc.exit_code) from exc
    except (ArchivedEmailNotFoundError, AnalysisNotFoundError, ReplyDraftNotFoundError) as exc:
        # The services intentionally use non-disclosing lookup failures: do not
        # reveal whether the requested object belongs to another account.
        _emit_error("not_found", "the requested workflow resource is unavailable")
        raise typer.Exit(code=1) from exc
    except (DraftVersionConflictError, DraftStateTransitionError) as exc:
        _emit_error("conflict", "the requested workflow transition is not valid")
        raise typer.Exit(code=1) from exc
    except TransientLLMError as exc:
        _emit_error("model_unavailable", "the model is temporarily unavailable")
        raise typer.Exit(code=1) from exc
    except NonRetryableLLMError as exc:
        _emit_error("model_request_failed", "the model could not process this workflow request")
        raise typer.Exit(code=1) from exc
    except MailWorkflowError as exc:
        _emit_error("workflow_failed", "the workflow request could not be completed")
        raise typer.Exit(code=1) from exc
    except SQLAlchemyError as exc:
        # Database drivers expose detailed SQL and connection diagnostics.  Do
        # not put those in the stable CLI output: they can reveal schema or
        # deployment details to a caller.  The parent class covers operational
        # and programming errors as well as other SQLAlchemy storage failures.
        _emit_error("storage_failure", "the workflow storage is unavailable or incompatible")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        _emit_error("invalid_request", "the workflow command configuration or input is invalid")
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        # Adapters can still raise untyped database or transport failures.  Keep
        # the CLI contract non-disclosing instead of printing their traceback.
        _emit_error("runtime_failure", "the workflow command could not be completed")
        raise typer.Exit(code=1) from exc

    _emit_json(result)


async def _execute_command(
    ctx: typer.Context,
    *,
    profile: str,
    required_role: str | None,
    requires_model: bool,
    operation: _WorkflowOperation[_WorkflowResult],
) -> _WorkflowResult:
    """Resolve trusted authority before creating a model-backed workflow service."""

    container = _container_from_context(ctx)
    try:
        principal = container.config.resolve_workflow_cli_profile(profile)
    except ValueError as exc:
        raise _CommandFailure(
            code="invalid_profile",
            message="the requested workflow profile is unavailable",
            exit_code=2,
        ) from exc

    if required_role is not None and required_role not in principal.roles:
        raise _CommandFailure(
            code="forbidden",
            message="the selected workflow profile is not allowed to run this command",
            exit_code=1,
        )

    if requires_model:
        try:
            gateway: LLMGateway = container.build_gemini_gateway()
        except ValueError as exc:
            raise _CommandFailure(
                code="configuration_error",
                message="the workflow model gateway is not configured",
                exit_code=2,
            ) from exc
    else:
        gateway = _NoModelGateway()

    workflow = container.build_mail_workflow(gateway)
    return await operation(workflow, principal)


def _container_from_context(ctx: typer.Context) -> _WorkflowContainer:
    """Read the container installed by the root CLI callback."""

    if ctx.obj is None:
        raise _CommandFailure(
            code="runtime_error",
            message="the workflow command was not initialized",
            exit_code=1,
        )
    return cast(_WorkflowContainer, ctx.obj)


def _emit_json(value: EmailAnalysis | ReplyDraft) -> None:
    """Emit one machine-readable result record on stdout."""

    if not is_dataclass(value):
        raise TypeError("workflow commands must return dataclass result records")
    typer.echo(
        json.dumps(
            asdict(value),
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _emit_error(code: str, message: str) -> None:
    """Emit a stable, non-sensitive machine-readable error on stderr."""

    typer.echo(
        json.dumps(
            {"error": {"code": code, "message": message}},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        err=True,
    )


def _json_default(value: object) -> str:
    """Serialize the only non-JSON values used in workflow result dataclasses."""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"cannot JSON serialize {type(value).__name__}")
