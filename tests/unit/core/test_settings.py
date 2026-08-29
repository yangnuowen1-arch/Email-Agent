import pytest

from app.core.settings import AppConfig, LLMConfig


@pytest.fixture(autouse=True)
def _isolate_local_dotenv(monkeypatch):
    """单元测试只验证显式设置的环境变量，不读取开发者本地配置。"""
    monkeypatch.setattr("app.core.settings.load_dotenv", lambda override=False: None)


def test_from_env_success_with_minimal_vars(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.delenv("DB_POOL_MIN_SIZE", raising=False)
    monkeypatch.delenv("DB_POOL_MAX_SIZE", raising=False)
    monkeypatch.delenv("LISTEN_IDLE_PING_SECONDS", raising=False)
    monkeypatch.delenv("LISTEN_BACKOFF_INITIAL_SECONDS", raising=False)
    monkeypatch.delenv("LISTEN_BACKOFF_MAX_SECONDS", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    cfg = AppConfig.from_env()
    assert cfg.database_url == "postgresql://user:pass@localhost:5432/db"
    assert cfg.db_pool_min_size == 1
    assert cfg.db_pool_max_size == 10
    assert cfg.listen_idle_ping_seconds == 60
    assert cfg.listen_backoff_initial_seconds == 1
    assert cfg.listen_backoff_max_seconds == 60
    assert cfg.log_level == "INFO"


def test_from_env_missing_database_url_raises_clear_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        AppConfig.from_env()


def test_from_env_empty_database_url_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(ValueError, match="DATABASE_URL"):
        AppConfig.from_env()


def test_from_env_invalid_pool_size_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "not-an-int")
    with pytest.raises(ValueError, match="DB_POOL_MAX_SIZE"):
        AppConfig.from_env()


def test_from_env_pool_min_greater_than_max_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "10")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "1")
    with pytest.raises(ValueError, match="DB_POOL"):
        AppConfig.from_env()


def test_from_env_custom_values_parsed(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "20")
    monkeypatch.setenv("LISTEN_IDLE_PING_SECONDS", "30")
    monkeypatch.setenv("LISTEN_BACKOFF_INITIAL_SECONDS", "2")
    monkeypatch.setenv("LISTEN_BACKOFF_MAX_SECONDS", "90")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    cfg = AppConfig.from_env()
    assert cfg.db_pool_min_size == 2
    assert cfg.db_pool_max_size == 20
    assert cfg.listen_idle_ping_seconds == 30
    assert cfg.listen_backoff_initial_seconds == 2
    assert cfg.listen_backoff_max_seconds == 90
    assert cfg.log_level == "DEBUG"


def test_from_env_zero_ping_seconds_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("LISTEN_IDLE_PING_SECONDS", "0")
    with pytest.raises(ValueError, match="LISTEN_IDLE_PING_SECONDS"):
        AppConfig.from_env()


def test_from_env_negative_backoff_initial_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("LISTEN_BACKOFF_INITIAL_SECONDS", "-1")
    with pytest.raises(ValueError, match="LISTEN_BACKOFF_INITIAL_SECONDS"):
        AppConfig.from_env()


def test_from_env_backoff_max_less_than_initial_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("LISTEN_BACKOFF_INITIAL_SECONDS", "30")
    monkeypatch.setenv("LISTEN_BACKOFF_MAX_SECONDS", "10")
    with pytest.raises(ValueError, match="LISTEN_BACKOFF_MAX_SECONDS"):
        AppConfig.from_env()


def test_from_env_without_database_url_when_not_required(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = AppConfig.from_env(require_database=False)
    assert cfg.database_url == ""
    assert isinstance(cfg.llm, LLMConfig)


def test_from_env_requires_database_url_by_default(monkeypatch):
    # 默认仍强制要求 DATABASE_URL，保持既有语义
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        AppConfig.from_env(require_database=True)
