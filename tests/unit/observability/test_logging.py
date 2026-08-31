import json
import logging
import sys

import pytest

from app.observability.logging import configure_logging


@pytest.fixture(autouse=True)
def _reset_logging_state(monkeypatch):
    """每个用例前重置模块级 _configured 标志与 root logger，保证隔离。"""
    monkeypatch.setattr("app.observability.logging._CONFIGURED", False)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def _make_record(name: str, level: int, msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="test_logging.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def _configured_handler():
    """找到 configure_logging 挂上的、带 ProcessorFormatter 的 handler。"""
    import structlog

    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler.formatter, structlog.stdlib.ProcessorFormatter):
            return handler
    msg = "no ProcessorFormatter handler found"
    raise AssertionError(msg)


def test_configure_logging_outputs_valid_json():
    configure_logging("INFO")

    handler = _configured_handler()
    assert handler.stream is sys.stderr
    record = _make_record("test.json", logging.INFO, "hello world")
    formatted = handler.format(record)
    parsed = json.loads(formatted)

    assert parsed["event"] == "hello world"
    assert parsed["level"] == "info"
    assert parsed["logger"] == "test.json"
    assert "timestamp" in parsed


def test_configured_structlog_event_is_rendered_once(capsys):
    import structlog

    configure_logging("INFO")
    structlog.get_logger("test.bound").info("workflow_started", email_id=42)

    parsed = json.loads(capsys.readouterr().err)
    assert parsed["event"] == "workflow_started"
    assert parsed["email_id"] == 42
    assert parsed["logger"] == "test.bound"


def test_configure_logging_formats_positional_args():
    """验证 %-style 位置参数被正确格式化进 event 字符串（与 ingest.py 用法一致）。"""
    configure_logging("INFO")

    handler = _configured_handler()
    record = logging.LogRecord(
        name="test.args",
        level=logging.INFO,
        pathname="test_logging.py",
        lineno=1,
        msg="ingest failed for %s (id=%s)",
        args=("myaccount", 42),
        exc_info=None,
    )
    formatted = handler.format(record)
    parsed = json.loads(formatted)

    assert parsed["event"] == "ingest failed for myaccount (id=42)"


def test_configure_logging_is_idempotent():
    configure_logging("DEBUG")
    configure_logging("ERROR")

    root = logging.getLogger()
    import structlog

    structlog_handlers = [
        h for h in root.handlers if isinstance(h.formatter, structlog.stdlib.ProcessorFormatter)
    ]
    assert len(structlog_handlers) == 1
    assert root.level == logging.DEBUG


def test_configure_logging_respects_level():
    configure_logging("WARNING")

    root = logging.getLogger()
    assert not root.isEnabledFor(logging.INFO)
    assert root.isEnabledFor(logging.WARNING)

    handler = _configured_handler()
    warn_record = _make_record("test.level", logging.WARNING, "should_appear")
    formatted = handler.format(warn_record)
    parsed = json.loads(formatted)
    assert parsed["event"] == "should_appear"
