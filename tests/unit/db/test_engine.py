from unittest.mock import MagicMock, patch

import pytest

from email_agent.config.settings import AppConfig
from email_agent.db import engine as engine_module


@pytest.fixture(autouse=True)
def _reset():
    engine_module.close_engine()
    yield
    engine_module.close_engine()


def _cfg(url="postgresql://u:p@localhost/db", min_size=1, max_size=5):
    return AppConfig(
        database_url=url,
        db_pool_min_size=min_size,
        db_pool_max_size=max_size,
    )


def test_get_engine_before_init_raises():
    with pytest.raises(RuntimeError, match="not initialized"):
        engine_module.get_engine()


def test_init_engine_creates_with_config():
    cfg = _cfg(min_size=2, max_size=10)
    with patch("email_agent.db.engine.create_engine") as mock_create:
        mock_eng = MagicMock()
        mock_create.return_value = mock_eng
        result = engine_module.init_engine(cfg)
        assert result is mock_eng
        # 连接池上限映射为 pool_size，硬上限 max_overflow=0
        kwargs = mock_create.call_args.kwargs
        assert kwargs["pool_size"] == 10
        assert kwargs["max_overflow"] == 0
        assert engine_module.get_engine() is mock_eng


def test_init_engine_wraps_error():
    with patch(
        "email_agent.db.engine.create_engine", side_effect=Exception("boom")
    ), pytest.raises(RuntimeError, match="failed to init DB engine"):
        engine_module.init_engine(_cfg())


def test_init_engine_twice_replaces_and_closes_old():
    with patch("email_agent.db.engine.create_engine") as mock_create:
        first = MagicMock()
        second = MagicMock()
        mock_create.side_effect = [first, second]
        engine_module.init_engine(_cfg())
        engine_module.init_engine(_cfg())
        first.dispose.assert_called_once()
        assert engine_module.get_engine() is second


def test_get_session_factory_before_init_raises():
    with pytest.raises(RuntimeError, match="not initialized"):
        engine_module.get_session_factory()


def test_get_session_factory_binds_engine():
    with patch("email_agent.db.engine.create_engine") as mock_create:
        mock_eng = MagicMock()
        mock_create.return_value = mock_eng
        engine_module.init_engine(_cfg())
        sf = engine_module.get_session_factory()
        assert sf.kw["bind"] is mock_eng


def test_close_engine_clears_and_disposes():
    with patch("email_agent.db.engine.create_engine") as mock_create:
        mock_eng = MagicMock()
        mock_create.return_value = mock_eng
        engine_module.init_engine(_cfg())
        engine_module.close_engine()
        assert engine_module._engine is None
        mock_eng.dispose.assert_called_once()
        with pytest.raises(RuntimeError, match="not initialized"):
            engine_module.get_engine()
