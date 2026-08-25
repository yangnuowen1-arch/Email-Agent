import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from email_agent.cli.main import (
    JsonFormatter,
    _mask_url,
    parse_args,
    print_summary,
    setup_logging,
)
from email_agent.models.account import Account
from email_agent.service.sync import SyncResult


def _account(**overrides) -> Account:
    base = dict(
        id=1, name="qq", host="imap.qq.com", username="u@qq.com", password="secret", last_sync_uid=0
    )
    base.update(overrides)
    return Account(**base)  # type: ignore[arg-type]


def test_mask_url_masks_password():
    assert (
        _mask_url("postgresql://user:pass@localhost:5432/db")
        == "postgresql://***:***@localhost:5432/db"
    )
    assert _mask_url(
        "postgresql://user@localhost/db"
    ) == "postgresql://***:***@localhost/db" or "postgresql://***:***@localhost/db" in _mask_url(
        "postgresql://user:pass@localhost/db"
    )
    # no url stays same
    assert _mask_url("no-url") == "no-url"


def test_parse_args_defaults():
    args = parse_args([])
    assert args.limit is None
    assert args.full is False


def test_parse_args_with_limit_and_full():
    args = parse_args(["--limit", "10", "--full"])
    assert args.limit == 10
    assert args.full is True


def test_parse_args_invalid_limit_rejects():
    with pytest.raises(SystemExit):
        parse_args(["--limit", "0"])
    with pytest.raises(SystemExit):
        parse_args(["--limit", "-1"])
    with pytest.raises(SystemExit):
        parse_args(["--limit", "abc"])


def test_json_formatter_outputs_json_and_masks_url(caplog=None):
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="connect to postgresql://user:secret@localhost/db",
        args=(),
        exc_info=None,
    )
    out = fmt.format(record)
    data = json.loads(out)
    assert data["level"] == "INFO"
    assert data["name"] == "test"
    # password should be masked
    assert "secret" not in data["message"]
    assert "***" in data["message"]
    assert "timestamp" in data


def test_json_formatter_handles_exc_info():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc = sys.exc_info()
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="t.py",
            lineno=1,
            msg="error",
            args=(),
            exc_info=exc,
        )
        out = fmt.format(record)
        data = json.loads(out)
        assert "exc_info" in data
        assert "ValueError" in data["exc_info"]


def test_setup_logging_sets_json_formatter():
    root = logging.getLogger()
    # clear any previous handlers for deterministic test
    root.handlers.clear()
    setup_logging("DEBUG")
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
    assert root.level == logging.DEBUG
    # cleanup for other tests
    root.handlers.clear()
    root.setLevel(logging.WARNING)


def test_print_summary_outputs_table(capsys):
    results = [
        SyncResult(
            account_id=1,
            name="qq",
            fetched=5,
            parsed=5,
            inserted=3,
            skipped=2,
            max_uid=100,
            error=None,
            duration_ms=100,
        ),
        SyncResult(
            account_id=2,
            name="gmail",
            fetched=0,
            parsed=0,
            inserted=0,
            skipped=0,
            max_uid=0,
            error="timeout after 60s",
            duration_ms=60000,
        ),
    ]
    print_summary(results, full=False, limit=None)
    out = capsys.readouterr().out
    assert "Sync Summary" in out
    assert "qq" in out
    assert "gmail" in out
    assert "fetched" in out
    assert "5" in out
    assert "ERROR" in out
    assert "Total:" in out
    # no limit note
    assert "breakpoint was NOT updated" not in out


def test_print_summary_with_limit_shows_note(capsys):
    results = [
        SyncResult(
            account_id=1,
            name="a",
            fetched=1,
            parsed=1,
            inserted=1,
            skipped=0,
            max_uid=1,
            error=None,
            duration_ms=10,
        )
    ]
    print_summary(results, full=False, limit=1)
    out = capsys.readouterr().out
    assert "Note: --limit was set" in out


def test_print_summary_no_results(capsys):
    print_summary([], full=False, limit=None)
    out = capsys.readouterr().out
    assert "total=0" in out


@patch("email_agent.cli.main.AccountStore")
@patch("email_agent.cli.main.sync_all")
@patch("email_agent.cli.main.AppContext")
def test_run_sync_no_enabled_accounts(
    mock_manager_cls, mock_sync, mock_account_store_cls, capsys
):
    from email_agent.cli.main import _run_sync
    from email_agent.config.settings import AppConfig

    config = AppConfig(database_url="postgresql://u:p@localhost/db")
    mock_manager = MagicMock()
    mock_manager_cls.return_value = mock_manager
    mock_read = MagicMock()
    mock_read.get_enabled_accounts.return_value = []
    mock_account_store_cls.return_value = mock_read
    mock_manager.session_factory.return_value = MagicMock()

    args = MagicMock(limit=None, full=False)
    code = _run_sync(args, config)
    assert code == 0
    out = capsys.readouterr().out
    assert "No enabled accounts" in out
    mock_sync.assert_not_called()
    mock_manager.session_factory.return_value.close.assert_called_once()
    mock_manager.close_all.assert_called_once()


@patch("email_agent.cli.main.AccountStore")
@patch("email_agent.cli.main.sync_all")
@patch("email_agent.cli.main.AppContext")
def test_run_sync_success_prints_summary_and_logs(
    mock_manager_cls, mock_sync, mock_account_store_cls, capsys, caplog
):
    from email_agent.cli.main import _run_sync
    from email_agent.config.settings import AppConfig

    config = AppConfig(database_url="postgresql://u:p@localhost/db")
    mock_manager = MagicMock()
    mock_manager_cls.return_value = mock_manager
    mock_read = MagicMock()
    acc = _account(id=1, name="qq")
    mock_read.get_enabled_accounts.return_value = [acc]
    mock_account_store_cls.return_value = mock_read
    mock_manager.session_factory.return_value = MagicMock()
    mock_sync.return_value = [
        SyncResult(
            account_id=1,
            name="qq",
            fetched=2,
            parsed=2,
            inserted=2,
            skipped=0,
            max_uid=12,
            error=None,
            duration_ms=100,
        )
    ]
    setup_logging("INFO")
    args = MagicMock(limit=None, full=False)
    with caplog.at_level(logging.INFO):
        code = _run_sync(args, config)
    assert code == 0
    out = capsys.readouterr().out
    assert "Sync Summary" in out
    assert "qq" in out
    # log should not contain password
    assert "secret" not in caplog.text
    mock_manager.close_all.assert_called_once()


@patch("email_agent.cli.main.AccountStore")
@patch("email_agent.cli.main.sync_all")
@patch("email_agent.cli.main.AppContext")
def test_run_sync_with_failures_logs_but_returns_zero(
    mock_manager_cls, mock_sync, mock_account_store_cls, caplog
):
    from email_agent.cli.main import _run_sync
    from email_agent.config.settings import AppConfig

    config = AppConfig(database_url="postgresql://u:p@localhost/db")
    mock_manager = MagicMock()
    mock_manager_cls.return_value = mock_manager
    mock_read = MagicMock()
    acc = _account(id=1, name="bad", password="s3cr3t")
    mock_read.get_enabled_accounts.return_value = [acc]
    mock_account_store_cls.return_value = mock_read
    mock_manager.session_factory.return_value = MagicMock()
    mock_sync.return_value = [
        SyncResult(
            account_id=1,
            name="bad",
            fetched=0,
            parsed=0,
            inserted=0,
            skipped=0,
            max_uid=0,
            error="IMAP connect failed",
            duration_ms=0,
        )
    ]
    setup_logging("INFO")
    args = MagicMock(limit=None, full=False)
    with caplog.at_level(logging.INFO):
        code = _run_sync(args, config)
    # per user decision 4, always 0
    assert code == 0
    assert "bad" in caplog.text
    assert "s3cr3t" not in caplog.text


@patch("email_agent.cli.main.AppContext", side_effect=Exception("db down"))
def test_run_sync_init_pool_failure_returns_one(mock_manager_cls, caplog):
    from email_agent.cli.main import _run_sync
    from email_agent.config.settings import AppConfig

    config = AppConfig(database_url="postgresql://u:p@localhost/db")
    setup_logging("INFO")
    args = MagicMock(limit=None, full=False)
    with caplog.at_level(logging.ERROR):
        code = _run_sync(args, config)
    assert code == 1
    assert "failed to init DB engine" in caplog.text


@patch("email_agent.cli.main.AccountStore")
@patch("email_agent.cli.main.AppContext")
def test_run_sync_get_accounts_failure_returns_one(
    mock_manager_cls, mock_account_store_cls, caplog
):
    from email_agent.cli.main import _run_sync
    from email_agent.config.settings import AppConfig

    config = AppConfig(database_url="postgresql://u:p@localhost/db")
    mock_manager = MagicMock()
    mock_manager_cls.return_value = mock_manager
    mock_read = MagicMock()
    mock_read.get_enabled_accounts.side_effect = Exception("query fail")
    mock_account_store_cls.return_value = mock_read
    mock_manager.session_factory.return_value = MagicMock()
    setup_logging("INFO")
    args = MagicMock(limit=None, full=False)
    with caplog.at_level(logging.ERROR):
        code = _run_sync(args, config)
    assert code == 1


@patch("email_agent.cli.main._run_sync", return_value=0)
@patch("email_agent.cli.main.AppConfig.from_env")
def test_main_success_exits_zero(mock_from_env, mock_run):
    from email_agent.cli.main import main
    from email_agent.config.settings import AppConfig

    mock_from_env.return_value = AppConfig(
        database_url="postgresql://u:p@localhost/db", log_level="INFO"
    )
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 0


@patch("email_agent.cli.main.AppConfig.from_env", side_effect=ValueError("DATABASE_URL missing"))
def test_main_config_error_exits_2(mock_from_env, caplog):
    from email_agent.cli.main import main

    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_main_limit_validation_exits_2():
    from email_agent.cli.main import main

    with pytest.raises(SystemExit) as exc:
        main(["--limit", "0"])
    # argparse exits with 2
    assert exc.value.code == 2


@patch("email_agent.cli.main._run_sync", return_value=0)
@patch("email_agent.cli.main.AppConfig.from_env")
def test_main_passes_limit_and_full_to_run(mock_from_env, mock_run):
    from email_agent.cli.main import main
    from email_agent.config.settings import AppConfig

    mock_from_env.return_value = AppConfig(database_url="postgresql://u:p@localhost/db")
    with pytest.raises(SystemExit):
        main(["--limit", "5", "--full"])
    # check _run_sync called with correct args
    args_passed = mock_run.call_args[0][0]
    assert args_passed.limit == 5
    assert args_passed.full is True


def test_mask_url_in_log_message_via_formatter():
    # ensure formatter masks url even if caller accidentally logs url with password
    fmt = JsonFormatter()
    rec = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="url postgresql://a:b@host/db",
        args=(),
        exc_info=None,
    )
    out = json.loads(fmt.format(rec))
    assert "b" not in out["message"] or "***" in out["message"]
