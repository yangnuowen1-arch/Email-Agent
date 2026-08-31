from app.core.container import Container
from app.core.settings import AppConfig


def test_build_gemini_gateway_passes_configured_timeout(monkeypatch):
    captured: dict[str, object] = {}

    class FakeGeminiGateway:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("app.core.container.GeminiLLMGateway", FakeGeminiGateway)
    container = Container(
        config=AppConfig(
            database_url="postgresql://u:p@localhost/db",
            gemini_api_key="test-gemini-key",
            gemini_model="gemini-custom-test-model",
            gemini_timeout_seconds=12.5,
        ),
        logger=object(),
        database=object(),
        inbox=object(),
        sync_store=object(),
        mail_sync=object(),
        mail_query_store=object(),
        mail_query=object(),
        mail_analysis_store=object(),
        reply_draft_store=object(),
    )

    gateway = container.build_gemini_gateway()

    assert isinstance(gateway, FakeGeminiGateway)
    assert captured == {
        "api_key": "test-gemini-key",
        "model": "gemini-custom-test-model",
        "timeout_seconds": 12.5,
    }
