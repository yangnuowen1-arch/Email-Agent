"""Tests for the local, profile-scoped human-review workflow CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from typer.testing import CliRunner

import app.cli.main as cli_main
from app.core.workflow_profiles import WorkflowPrincipal
from app.llm.errors import TransientLLMError
from app.ports.mail_workflow import DraftVersionConflictError
from app.schemas import (
    DraftDecision,
    DraftStatus,
    EmailAnalysis,
    MailIntent,
    MailUrgency,
    ReplyDraft,
)

_NOW = datetime(2026, 8, 31, 9, 30, tzinfo=UTC)


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


class _Config:
    """Minimal root-callback config plus a trusted profile resolver."""

    log_level = "INFO"
    db_pool_min_size = 1
    db_pool_max_size = 2

    def __init__(self, principals: dict[str, WorkflowPrincipal]) -> None:
        self._principals = principals
        self.resolved_profiles: list[str] = []

    def resolve_workflow_cli_profile(self, profile_name: str) -> WorkflowPrincipal:
        self.resolved_profiles.append(profile_name)
        try:
            return self._principals[profile_name]
        except KeyError as exc:
            raise ValueError(f"untrusted profile diagnostic: {profile_name}") from exc


class _AnalysisService:
    def __init__(self, result: EmailAnalysis | Exception) -> None:
        self.result = result
        self.calls: list[tuple[int, frozenset[int]]] = []

    async def analyze(self, email_id: int, *, allowed_account_ids: frozenset[int]) -> EmailAnalysis:
        self.calls.append((email_id, allowed_account_ids))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _DraftService:
    def __init__(self, *, result: ReplyDraft, decide_error: Exception | None = None) -> None:
        self.result = result
        self.decide_error = decide_error
        self.create_calls: list[tuple[int, str, str, frozenset[int]]] = []
        self.decide_calls: list[
            tuple[str, DraftDecision, str, int, frozenset[int], str | None]
        ] = []
        self.get_current_calls: list[tuple[str, frozenset[int]]] = []

    async def create(
        self,
        email_id: int,
        analysis_id: str,
        *,
        actor_id: str,
        allowed_account_ids: frozenset[int],
    ) -> ReplyDraft:
        self.create_calls.append((email_id, analysis_id, actor_id, allowed_account_ids))
        return self.result

    async def decide(
        self,
        draft_id: str,
        decision: DraftDecision,
        *,
        actor_id: str,
        expected_version: int,
        allowed_account_ids: frozenset[int],
        comment: str | None = None,
    ) -> ReplyDraft:
        self.decide_calls.append(
            (draft_id, decision, actor_id, expected_version, allowed_account_ids, comment)
        )
        if self.decide_error is not None:
            raise self.decide_error
        return self.result

    async def get_current(
        self,
        draft_id: str,
        *,
        allowed_account_ids: frozenset[int],
    ) -> ReplyDraft:
        self.get_current_calls.append((draft_id, allowed_account_ids))
        return self.result


class _WorkflowServices:
    def __init__(self, analysis: _AnalysisService, drafts: _DraftService) -> None:
        self.analysis = analysis
        self.drafts = drafts


class _Container:
    def __init__(
        self,
        *,
        config: _Config,
        workflow: _WorkflowServices,
        gateway_error: Exception | None = None,
    ) -> None:
        self.config = config
        self.logger = _Logger()
        self.workflow = workflow
        self.gateway_error = gateway_error
        self.gateway_calls = 0
        self.workflow_build_calls: list[object] = []
        self.close_calls = 0

    def build_gemini_gateway(self) -> object:
        self.gateway_calls += 1
        if self.gateway_error is not None:
            raise self.gateway_error
        return object()

    def build_mail_workflow(self, gateway: object) -> _WorkflowServices:
        self.workflow_build_calls.append(gateway)
        return self.workflow

    async def close_all(self) -> None:
        self.close_calls += 1


def _principal(*, actor_id: str, roles: set[str], accounts: set[int]) -> WorkflowPrincipal:
    return WorkflowPrincipal(
        actor_id=actor_id,
        roles=frozenset(roles),
        allowed_account_ids=frozenset(accounts),
    )


def _analysis() -> EmailAnalysis:
    return EmailAnalysis(
        analysis_id="analysis-1",
        email_id=42,
        account_id=7,
        summary="Customer needs a reply about the requested update.",
        intent=MailIntent.REQUEST,
        urgency=MailUrgency.NORMAL,
        reply_required=True,
        analyzed_at=_NOW,
        key_points=("Needs an update",),
        action_items=("Prepare a response",),
    )


def _draft(
    *,
    status: DraftStatus = DraftStatus.DRAFT,
    version: int = 1,
    review_comment: str | None = None,
) -> ReplyDraft:
    reviewed = status is DraftStatus.APPROVED
    return ReplyDraft(
        draft_id="draft-1",
        email_id=42,
        account_id=7,
        analysis_id="analysis-1",
        version=version,
        status=status,
        recipients=("customer@example.com",),
        subject="Re: requested update",
        body_text="Thanks for your message. We will follow up shortly.",
        created_at=_NOW,
        updated_at=_NOW,
        created_by="author-1",
        updated_by="reviewer-1" if reviewed else "author-1",
        reviewed_by="reviewer-1" if reviewed else None,
        reviewed_at=_NOW if reviewed else None,
        review_comment=review_comment if reviewed else None,
    )


def _install_fake_container(monkeypatch: pytest.MonkeyPatch, container: _Container) -> None:
    monkeypatch.setattr(
        cli_main.AppConfig,
        "from_env",
        classmethod(lambda _cls: container.config),
    )
    monkeypatch.setattr(cli_main, "build_container", lambda _config: container)


def _make_container(
    *,
    principals: dict[str, WorkflowPrincipal],
    analysis_result: EmailAnalysis | Exception | None = None,
    draft_result: ReplyDraft | None = None,
    decide_error: Exception | None = None,
    gateway_error: Exception | None = None,
) -> tuple[_Container, _AnalysisService, _DraftService]:
    analysis = _AnalysisService(analysis_result or _analysis())
    drafts = _DraftService(result=draft_result or _draft(), decide_error=decide_error)
    workflow = _WorkflowServices(analysis, drafts)
    return (
        _Container(
            config=_Config(principals),
            workflow=workflow,
            gateway_error=gateway_error,
        ),
        analysis,
        drafts,
    )


def _error(result) -> dict[str, object]:
    return json.loads(result.stderr)


def test_analyze_forwards_profile_scope_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _principal(actor_id="author-1", roles={"author"}, accounts={7, 9})
    container, analysis, _drafts = _make_container(principals={"author": author})
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(
        cli_main.app,
        ["workflow", "analyze", "--profile", "author", "--email-id", "42"],
    )

    assert result.exit_code == 0
    assert analysis.calls == [(42, frozenset({7, 9}))]
    assert json.loads(result.stdout) == {
        "account_id": 7,
        "action_items": ["Prepare a response"],
        "analysis_id": "analysis-1",
        "analyzed_at": "2026-08-31T09:30:00+00:00",
        "email_id": 42,
        "intent": "request",
        "key_points": ["Needs an update"],
        "reply_required": True,
        "summary": "Customer needs a reply about the requested update.",
        "urgency": "normal",
    }
    assert container.gateway_calls == 1
    assert container.close_calls == 1


def test_author_commands_forward_create_and_submit_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _principal(actor_id="author-1", roles={"author"}, accounts={7})
    container, _analysis_service, drafts = _make_container(principals={"author": author})
    _install_fake_container(monkeypatch, container)
    runner = CliRunner()

    create_result = runner.invoke(
        cli_main.app,
        [
            "workflow",
            "create-draft",
            "--profile",
            "author",
            "--email-id",
            "42",
            "--analysis-id",
            "analysis-1",
        ],
    )
    submit_result = runner.invoke(
        cli_main.app,
        [
            "workflow",
            "submit",
            "--profile",
            "author",
            "--draft-id",
            "draft-1",
            "--expected-version",
            "1",
        ],
    )

    assert create_result.exit_code == 0
    assert submit_result.exit_code == 0
    assert drafts.create_calls == [(42, "analysis-1", "author-1", frozenset({7}))]
    assert drafts.decide_calls == [
        ("draft-1", DraftDecision.SUBMIT_FOR_REVIEW, "author-1", 1, frozenset({7}), None)
    ]
    # Draft creation needs the model; submitting an existing draft does not.
    assert container.gateway_calls == 1
    assert container.close_calls == 2


def test_reviewer_can_show_and_approve_with_review_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _principal(actor_id="reviewer-1", roles={"reviewer"}, accounts={7, 8})
    container, _analysis_service, drafts = _make_container(
        principals={"reviewer": reviewer},
        draft_result=_draft(
            status=DraftStatus.APPROVED,
            version=3,
            review_comment="Contents verified",
        ),
    )
    _install_fake_container(monkeypatch, container)
    runner = CliRunner()

    show_result = runner.invoke(
        cli_main.app,
        ["workflow", "show", "--profile", "reviewer", "--draft-id", "draft-1"],
    )
    approve_result = runner.invoke(
        cli_main.app,
        [
            "workflow",
            "approve",
            "--profile",
            "reviewer",
            "--draft-id",
            "draft-1",
            "--expected-version",
            "2",
            "--comment",
            "Contents verified",
        ],
    )

    assert show_result.exit_code == 0
    assert approve_result.exit_code == 0
    assert drafts.get_current_calls == [("draft-1", frozenset({7, 8}))]
    assert drafts.decide_calls == [
        (
            "draft-1",
            DraftDecision.APPROVE,
            "reviewer-1",
            2,
            frozenset({7, 8}),
            "Contents verified",
        )
    ]
    # Viewing and approving an existing draft must not depend on Gemini.
    assert container.gateway_calls == 0
    assert container.close_calls == 2


def test_author_cannot_approve_before_constructing_a_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _principal(actor_id="author-1", roles={"author"}, accounts={7})
    container, _analysis_service, _drafts = _make_container(principals={"author": author})
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "workflow",
            "approve",
            "--profile",
            "author",
            "--draft-id",
            "draft-1",
            "--expected-version",
            "2",
        ],
    )

    assert result.exit_code == 1
    assert _error(result) == {
        "error": {
            "code": "forbidden",
            "message": "the selected workflow profile is not allowed to run this command",
        }
    }
    assert container.gateway_calls == 0
    assert container.workflow_build_calls == []
    assert container.close_calls == 1


def test_unknown_profile_has_a_stable_non_disclosing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _principal(actor_id="author-1", roles={"author"}, accounts={7})
    container, _analysis_service, _drafts = _make_container(principals={"author": author})
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(
        cli_main.app,
        ["workflow", "analyze", "--profile", "missing", "--email-id", "42"],
    )

    assert result.exit_code == 2
    assert _error(result) == {
        "error": {
            "code": "invalid_profile",
            "message": "the requested workflow profile is unavailable",
        }
    }
    assert "untrusted profile diagnostic" not in result.stderr
    assert container.gateway_calls == 0
    assert container.close_calls == 1


def test_transient_model_error_is_non_sensitive_and_closes_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _principal(actor_id="author-1", roles={"author"}, accounts={7})
    container, _analysis_service, _drafts = _make_container(
        principals={"author": author},
        analysis_result=TransientLLMError("provider response leaked-secret-123"),
    )
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(
        cli_main.app,
        ["workflow", "analyze", "--profile", "author", "--email-id", "42"],
    )

    assert result.exit_code == 1
    assert _error(result) == {
        "error": {
            "code": "model_unavailable",
            "message": "the model is temporarily unavailable",
        }
    }
    assert "leaked-secret-123" not in result.stderr
    assert container.close_calls == 1


@pytest.mark.parametrize(
    "storage_error",
    [
        SQLAlchemyError("storage diagnostic leaked-secret-123"),
        ProgrammingError("INSERT INTO email_analyses", {}, RuntimeError("missing table")),
        OperationalError("INSERT INTO email_analyses", {}, RuntimeError("connection refused")),
    ],
)
def test_sqlalchemy_storage_errors_are_non_disclosing_and_stable(
    monkeypatch: pytest.MonkeyPatch,
    storage_error: SQLAlchemyError,
) -> None:
    author = _principal(actor_id="author-1", roles={"author"}, accounts={7})
    container, _analysis_service, _drafts = _make_container(
        principals={"author": author},
        analysis_result=storage_error,
    )
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(
        cli_main.app,
        ["workflow", "analyze", "--profile", "author", "--email-id", "42"],
    )

    assert result.exit_code == 1
    assert _error(result) == {
        "error": {
            "code": "storage_failure",
            "message": "the workflow storage is unavailable or incompatible",
        }
    }
    assert "leaked-secret-123" not in result.stderr
    assert "email_analyses" not in result.stderr
    assert container.close_calls == 1


def test_draft_version_conflict_maps_to_a_stable_conflict_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _principal(actor_id="author-1", roles={"author"}, accounts={7})
    container, _analysis_service, drafts = _make_container(
        principals={"author": author},
        decide_error=DraftVersionConflictError("expected version 1, actual version 2"),
    )
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "workflow",
            "submit",
            "--profile",
            "author",
            "--draft-id",
            "draft-1",
            "--expected-version",
            "1",
        ],
    )

    assert result.exit_code == 1
    assert _error(result) == {
        "error": {
            "code": "conflict",
            "message": "the requested workflow transition is not valid",
        }
    }
    assert drafts.decide_calls == [
        ("draft-1", DraftDecision.SUBMIT_FOR_REVIEW, "author-1", 1, frozenset({7}), None)
    ]
    assert container.close_calls == 1


def test_non_generation_commands_work_without_a_configured_model_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _principal(actor_id="reviewer-1", roles={"reviewer"}, accounts={7})
    container, _analysis_service, drafts = _make_container(
        principals={"reviewer": reviewer},
        gateway_error=ValueError("GEMINI_API_KEY is missing"),
    )
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(
        cli_main.app,
        ["workflow", "show", "--profile", "reviewer", "--draft-id", "draft-1"],
    )

    assert result.exit_code == 0
    assert drafts.get_current_calls == [("draft-1", frozenset({7}))]
    assert container.gateway_calls == 0
    assert container.close_calls == 1


def test_untyped_runtime_failure_is_non_disclosing_and_closes_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _principal(actor_id="author-1", roles={"author"}, accounts={7})
    container, _analysis_service, _drafts = _make_container(
        principals={"author": author},
        analysis_result=RuntimeError("postgres://secret-host/internal-details"),
    )
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(
        cli_main.app,
        ["workflow", "analyze", "--profile", "author", "--email-id", "42"],
    )

    assert result.exit_code == 1
    assert _error(result) == {
        "error": {
            "code": "runtime_failure",
            "message": "the workflow command could not be completed",
        }
    }
    assert "secret-host" not in result.stderr
    assert container.close_calls == 1


def test_workflow_help_and_parse_errors_close_the_root_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _principal(actor_id="author-1", roles={"author"}, accounts={7})
    container, _analysis_service, _drafts = _make_container(principals={"author": author})
    _install_fake_container(monkeypatch, container)
    runner = CliRunner()

    help_result = runner.invoke(cli_main.app, ["workflow", "--help"])
    parse_result = runner.invoke(
        cli_main.app,
        ["workflow", "analyze", "--profile", "author"],
    )

    assert help_result.exit_code == 0
    assert parse_result.exit_code == 2
    assert container.close_calls == 2
