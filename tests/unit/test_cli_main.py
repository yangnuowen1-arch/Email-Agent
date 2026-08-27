"""CLI options and lifecycle tests for the mail sync entry point."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import app.cli.main as cli_main
from app.schemas import BatchResult


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


class _Coordinator:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[bool, int | None]] = []
        self.error = error

    async def ingest_accounts(self, *, full: bool, limit: int | None) -> BatchResult:
        self.calls.append((full, limit))
        if self.error is not None:
            raise self.error
        return BatchResult(total_inserted=2, total_skipped=1, duration_ms=12)


class _Container:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.logger = _Logger()
        self.coordinator = _Coordinator(error=error)
        self.close_calls = 0

    async def close_all(self) -> None:
        self.close_calls += 1


def _install_fake_container(monkeypatch: pytest.MonkeyPatch, container: _Container) -> None:
    config = SimpleNamespace(log_level="INFO", db_pool_min_size=1, db_pool_max_size=1)
    monkeypatch.setattr(
        cli_main.AppConfig,
        "from_env",
        classmethod(lambda _cls: config),
    )
    monkeypatch.setattr(cli_main, "Container", lambda _config: container)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], (False, None)),
        (["--full"], (True, None)),
        (["--limit", "20"], (False, 20)),
        (["--full", "--limit", "20"], (True, 20)),
    ],
)
def test_ingest_forwards_sync_options(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    expected: tuple[bool, int | None],
) -> None:
    container = _Container()
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(cli_main.app, ["ingest", *args])

    assert result.exit_code == 0
    assert container.coordinator.calls == [expected]
    assert container.close_calls == 1
    assert "inserted=2 skipped=1 failed=0 duration_ms=12" in result.stdout


def test_bare_command_shows_help_without_building_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fail_if_loaded(_cls):
        nonlocal called
        called = True
        raise AssertionError("bare command must not load configuration")

    monkeypatch.setattr(cli_main.AppConfig, "from_env", classmethod(fail_if_loaded))

    result = CliRunner().invoke(cli_main.app, [])

    assert result.exit_code == 0
    assert "ingest" in result.stdout
    assert called is False


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_ingest_rejects_invalid_limit(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    container = _Container()
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(cli_main.app, ["ingest", "--limit", value])

    assert result.exit_code == 2
    assert container.coordinator.calls == []


def test_ingest_closes_container_after_fatal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _Container(error=RuntimeError("boom"))
    _install_fake_container(monkeypatch, container)

    result = CliRunner().invoke(cli_main.app, ["ingest"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert container.close_calls == 1
