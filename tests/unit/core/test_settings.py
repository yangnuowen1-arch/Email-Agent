import pytest

from app.core.settings import AppConfig
from app.core.workflow_profiles import WorkflowPrincipal


@pytest.fixture(autouse=True)
def _isolate_local_dotenv(monkeypatch):
    """单元测试只验证显式设置的环境变量，不读取开发者本地配置。"""
    monkeypatch.setattr("app.core.settings.load_dotenv", lambda override=False: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("WORKFLOW_CLI_PROFILES_JSON", raising=False)


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
    assert cfg.gemini_model == "gemini-3.6-flash"
    assert cfg.gemini_timeout_seconds == 30.0
    assert dict(cfg.workflow_cli_profiles) == {}


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
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom-test-model")
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "12.5")

    cfg = AppConfig.from_env()

    assert cfg.gemini_api_key == "test-gemini-key"
    assert cfg.gemini_model == "gemini-custom-test-model"
    assert cfg.gemini_timeout_seconds == 12.5


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-0.5", "nan", "inf"])
def test_from_env_invalid_gemini_timeout_raises_clear_error(monkeypatch, raw):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", raw)

    with pytest.raises(ValueError, match="GEMINI_TIMEOUT_SECONDS must be a positive finite number"):
        AppConfig.from_env()


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


def test_from_env_parses_immutable_workflow_cli_profiles(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv(
        "WORKFLOW_CLI_PROFILES_JSON",
        """
        {
          "author": {
            "actor_id": "alice",
            "roles": ["author"],
            "allowed_account_ids": [1, 2]
          },
          "reviewer": {
            "actor_id": "bob",
            "roles": ["reviewer"],
            "allowed_account_ids": [2]
          }
        }
        """,
    )

    cfg = AppConfig.from_env()

    author = cfg.resolve_workflow_cli_profile("author")
    assert author == WorkflowPrincipal(
        actor_id="alice", roles=frozenset({"author"}), allowed_account_ids=frozenset({1, 2})
    )
    assert cfg.resolve_workflow_cli_profile("reviewer").actor_id == "bob"
    with pytest.raises(TypeError):
        cfg.workflow_cli_profiles["other"] = author  # type: ignore[index]


def test_resolve_workflow_cli_profile_unknown_name_is_stable_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    cfg = AppConfig.from_env()

    with pytest.raises(ValueError, match=r"^unknown workflow CLI profile: 'missing'$"):
        cfg.resolve_workflow_cli_profile("missing")


@pytest.mark.parametrize(
    ("raw_profiles", "error"),
    [
        ("[]", "must be a JSON object"),
        ("{", "must be valid JSON"),
        (
            '{"bad profile":{"actor_id":"alice","roles":["author"],"allowed_account_ids":[1]}}',
            "invalid profile name",
        ),
        (
            '{"author":{"actor_id":" ","roles":["author"],"allowed_account_ids":[1]}}',
            "actor_id must be a non-empty string",
        ),
        (
            '{"author":{"actor_id":"alice","roles":["admin"],"allowed_account_ids":[1]}}',
            "roles must contain only author or reviewer",
        ),
        (
            '{"author":{"actor_id":"alice","roles":["author","author"],"allowed_account_ids":[1]}}',
            "roles must not contain duplicates",
        ),
        (
            '{"author":{"actor_id":"alice","roles":["author"],"allowed_account_ids":[0]}}',
            "allowed_account_ids must contain positive integers",
        ),
        (
            '{"author":{"actor_id":"alice","roles":["author"],"allowed_account_ids":[1,1]}}',
            "allowed_account_ids must not contain duplicates",
        ),
        (
            '{"author":{"actor_id":"alice","roles":["author"],"allowed_account_ids":[1],"extra":true}}',
            "has unknown fields: extra",
        ),
        (
            '{"author":{"actor_id":"alice","actor_id":"bob","roles":["author"],"allowed_account_ids":[1]}}',
            "must not contain duplicate fields",
        ),
    ],
)
def test_invalid_workflow_cli_profiles_do_not_block_unrelated_configuration(
    monkeypatch, raw_profiles, error
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("WORKFLOW_CLI_PROFILES_JSON", raw_profiles)

    cfg = AppConfig.from_env()

    with pytest.raises(ValueError, match=error):
        cfg.resolve_workflow_cli_profile("author")
