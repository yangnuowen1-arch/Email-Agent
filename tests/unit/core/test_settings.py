import pytest

from app.core.settings import AppConfig


@pytest.fixture(autouse=True)
def _isolate_local_dotenv(monkeypatch):
    """单元测试只验证显式设置的环境变量，不读取开发者本地配置。"""
    monkeypatch.setattr("app.core.settings.load_dotenv", lambda override=False: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)


def test_from_env_success_with_minimal_vars(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.delenv("DB_POOL_MIN_SIZE", raising=False)
    monkeypatch.delenv("DB_POOL_MAX_SIZE", raising=False)
    monkeypatch.delenv("SYNC_MAX_WORKERS", raising=False)
    monkeypatch.delenv("SYNC_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    cfg = AppConfig.from_env()
    assert cfg.database_url == "postgresql://user:pass@localhost:5432/db"
    assert cfg.db_pool_min_size == 1
    assert cfg.db_pool_max_size == 10
    assert cfg.sync_max_workers == 5
    assert cfg.sync_timeout_seconds == 60
    assert cfg.log_level == "INFO"
    assert cfg.gemini_api_key is None
    assert cfg.gemini_model == "gemini-2.5-flash"


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
    monkeypatch.setenv("SYNC_MAX_WORKERS", "8")
    monkeypatch.setenv("SYNC_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    cfg = AppConfig.from_env()
    assert cfg.db_pool_min_size == 2
    assert cfg.db_pool_max_size == 20
    assert cfg.sync_max_workers == 8
    assert cfg.sync_timeout_seconds == 120
    assert cfg.log_level == "DEBUG"


def test_from_env_reads_optional_gemini_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")

    cfg = AppConfig.from_env()

    assert cfg.gemini_api_key == "test-gemini-key"
    assert cfg.gemini_model == "gemini-2.5-pro"


def test_from_env_zero_workers_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("SYNC_MAX_WORKERS", "0")
    with pytest.raises(ValueError, match="SYNC_MAX_WORKERS"):
        AppConfig.from_env()


def test_from_env_negative_timeout_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("SYNC_TIMEOUT_SECONDS", "-1")
    with pytest.raises(ValueError, match="SYNC_TIMEOUT_SECONDS"):
        AppConfig.from_env()
