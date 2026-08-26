"""providers/email 包自包含性回归测试：本包不得依赖 app.models。"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

from app.providers.email.base import AccountConfig
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


def test_account_config_defaults_match_account_contract():
    cfg = _config()

    assert cfg.port == 993
    assert cfg.protocol == "imap"
    assert cfg.use_ssl is True
    assert cfg.folder == "INBOX"


def test_imap_client_works_with_pure_config_without_orm_account():
    mock_instance = MagicMock()
    mock_instance.search.return_value = [7]
    mock_instance.fetch.return_value = {7: {b"RFC822": b"raw"}}
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(_config(), client_cls=mock_cls)
    client.connect()
    result = client.fetch_emails("INBOX", since_uid=5)

    assert result == [(7, b"raw")]
    mock_instance.login.assert_called_once_with("u@example.com", "s3cr3t")


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
