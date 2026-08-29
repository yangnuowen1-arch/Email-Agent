"""providers/email 包自包含性回归测试与 IMAP 阻塞接收行为测试。

本包不得依赖 app.models；ImapMailClient.receive_emails 的基线/确认/重连
语义通过注入 MagicMock 客户端隔离网络验证。
"""

import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from imapclient.exceptions import LoginError

from app.providers.email.base import AccountConfig, MailClientAuthError
from app.providers.email.imap.client import ImapMailClient


def _config(**overrides) -> AccountConfig:
    base = dict(
        name="test-account",
        host="imap.example.com",
        username="u@example.com",
        password="s3cr3t",
    )
    base.update(overrides)
    return AccountConfig(**base)


def _client(mock_instance, **kwargs) -> ImapMailClient:
    mock_cls = MagicMock(return_value=mock_instance)
    return ImapMailClient(_config(), client_cls=mock_cls, **kwargs)


def test_account_config_defaults_match_account_contract():
    cfg = _config()

    assert cfg.port == 993
    assert cfg.protocol == "imap"
    assert cfg.use_ssl is True
    assert cfg.folder == "INBOX"


def test_receive_emails_pushes_new_mail_and_advances():
    mock_instance = MagicMock()
    mock_instance.has_capability.return_value = True
    # 第一次 search 为基线（存量最大 uid=10），之后两轮各发现一封新邮件
    mock_instance.search.side_effect = [[10], [11], [12]]
    mock_instance.fetch.side_effect = [
        {11: {b"RFC822": b"raw1"}},
        {12: {b"RFC822": b"raw2"}},
    ]
    mock_instance.idle_check.side_effect = [
        [(b"EXISTS", 11)],
        [(b"EXISTS", 12)],
        [],
    ]

    stop_event = threading.Event()
    batches: list[list[tuple[int, bytes]]] = []

    def on_batch(batch):
        batches.append(batch)
        if len(batches) >= 2:
            stop_event.set()

    client = _client(mock_instance, idle_ping_interval=0.01)
    client.receive_emails("INBOX", on_batch, stop_event=stop_event)

    assert batches == [[(11, b"raw1")], [(12, b"raw2")]]
    # 基线取存量最大 uid，之后每轮只搜索断点之后的 uid
    assert mock_instance.search.call_args_list == [
        call(["ALL"]),
        call(["UID", "11:*"]),
        call(["UID", "12:*"]),
    ]
    mock_instance.login.assert_called_once_with("u@example.com", "s3cr3t")
    assert mock_instance.login.call_count == 1  # 全程仅一条连接
    mock_instance.logout.assert_called_once()  # 退出时释放连接


def test_receive_emails_repushes_batch_when_callback_fails():
    mock_instance = MagicMock()
    mock_instance.has_capability.return_value = True
    # 回调失败不推进断点：同一 uid 被搜索并推送两次
    mock_instance.search.side_effect = [[10], [11], [11]]
    raw_fetch = {11: {b"RFC822": b"raw1"}}
    mock_instance.fetch.side_effect = [raw_fetch, raw_fetch]
    mock_instance.idle_check.side_effect = [[], []]

    stop_event = threading.Event()
    attempts: list[list[tuple[int, bytes]]] = []

    def on_batch(batch):
        attempts.append(batch)
        if len(attempts) == 1:
            raise RuntimeError("store down")
        stop_event.set()

    client = _client(
        mock_instance,
        idle_ping_interval=0.01,
        backoff_initial_seconds=0.01,
        backoff_max_seconds=0.02,
    )
    client.receive_emails("INBOX", on_batch, stop_event=stop_event)

    # 第一批回调失败后原样重推，不丢也不跳
    assert attempts == [[(11, b"raw1")], [(11, b"raw1")]]


def test_receive_emails_stops_on_stop_event():
    mock_instance = MagicMock()
    mock_instance.has_capability.return_value = True
    mock_instance.search.return_value = [5]

    stop_event = threading.Event()

    def _idle_check(timeout):
        stop_event.set()
        return []

    mock_instance.idle_check.side_effect = _idle_check

    client = _client(mock_instance, idle_ping_interval=0.01)
    client.receive_emails("INBOX", lambda batch: None, stop_event=stop_event)

    # 基线之后未发现新邮件即退出，无任何推送
    mock_instance.fetch.assert_not_called()
    mock_instance.logout.assert_called_once()


def test_receive_emails_reconnects_and_resumes_from_checkpoint():
    inst1 = MagicMock()
    inst1.has_capability.return_value = True
    inst1.search.return_value = [10]
    inst1.idle_check.side_effect = ConnectionError("connection reset by peer")

    inst2 = MagicMock()
    inst2.has_capability.return_value = True
    inst2.search.return_value = [11]
    inst2.fetch.return_value = {11: {b"RFC822": b"raw11"}}
    inst2.idle_check.side_effect = [[]]

    mock_cls = MagicMock(side_effect=[inst1, inst2])

    stop_event = threading.Event()
    batches: list[list[tuple[int, bytes]]] = []

    def on_batch(batch):
        batches.append(batch)
        stop_event.set()

    client = ImapMailClient(
        _config(),
        client_cls=mock_cls,
        idle_ping_interval=0.01,
        backoff_initial_seconds=0.01,
    )
    client.receive_emails("INBOX", on_batch, stop_event=stop_event)

    assert batches == [[(11, b"raw11")]]
    assert mock_cls.call_count == 2  # 故障后重建了一条连接
    inst1.logout.assert_called_once()
    # 重连后从已确认断点继续，而不是重新取基线
    inst2.search.assert_called_once_with(["UID", "11:*"])


def test_receive_emails_raises_auth_error_on_login_failure():
    mock_instance = MagicMock()
    mock_instance.login.side_effect = LoginError("invalid credentials")

    client = _client(mock_instance)
    with pytest.raises(MailClientAuthError):
        client.receive_emails("INBOX", lambda batch: None)


def test_receive_emails_polls_without_idle_capability():
    mock_instance = MagicMock()
    mock_instance.has_capability.return_value = False
    # 基线 → 空轮询 → 发现新邮件
    mock_instance.search.side_effect = [[5], [], [6]]
    mock_instance.fetch.return_value = {6: {b"RFC822": b"raw6"}}

    stop_event = threading.Event()
    batches: list[list[tuple[int, bytes]]] = []

    def on_batch(batch):
        batches.append(batch)
        stop_event.set()

    client = _client(mock_instance, idle_ping_interval=0.01)
    client.receive_emails("INBOX", on_batch, stop_event=stop_event)

    assert batches == [[(6, b"raw6")]]
    mock_instance.idle.assert_not_called()
    mock_instance.idle_check.assert_not_called()


def test_receive_emails_rejects_non_callable_callback():
    client = _client(MagicMock())
    with pytest.raises(ValueError, match="on_batch"):
        client.receive_emails("INBOX", "not-callable")  # type: ignore[arg-type]


def test_import_email_provider_does_not_load_models():
    # 在干净解释器中导入本包后，models 模块不应被加载（无跨包依赖）
    code = "import app.providers.email, sys; print('app.models' in sys.modules)"
    source_root = Path(__file__).resolve().parents[4] / "backend"
    env = {
        **os.environ,
        "PYTHONPATH": str(source_root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )

    assert result.stdout.strip() == "False"
