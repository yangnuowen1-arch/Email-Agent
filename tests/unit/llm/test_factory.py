"""LLM factory 单元测试：验证 build_chat_model / build_embedding_model 参数映射与配置校验。"""

from __future__ import annotations

import pytest
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.core.settings import LLMConfig
from app.llm.errors import LLMConfigurationError
from app.llm.factory import build_chat_model, build_embedding_model


def test_build_chat_model_requires_api_key() -> None:
    with pytest.raises(LLMConfigurationError, match="No API key"):
        build_chat_model(LLMConfig(llm_api_key=None))


def test_build_chat_model_empty_key_raises() -> None:
    with pytest.raises(LLMConfigurationError, match="No API key"):
        build_chat_model(LLMConfig(llm_api_key=""))


def test_build_chat_model_returns_chat_openai() -> None:
    cfg = LLMConfig(llm_api_key="test-key-123")
    model = build_chat_model(cfg)
    assert isinstance(model, ChatOpenAI)


def test_build_chat_model_maps_parameters() -> None:
    cfg = LLMConfig(
        llm_api_key="test-key-123",
        llm_model="gpt-4o-mini",
        llm_base_url="https://custom.api/v1",
        llm_temperature=0.5,
        llm_max_tokens=2048,
        llm_timeout_seconds=60,
    )
    model = build_chat_model(cfg)

    assert model.model_name == "gpt-4o-mini"
    assert model.temperature == pytest.approx(0.5)
    assert model.max_tokens == 2048
    # langchain-openai 将 timeout 映射为 request_timeout
    assert model.request_timeout == 60
    # 验证 base_url 被设置（langchain-openai 内部字段名）
    assert "custom.api" in str(model.openai_api_base)


class TestBuildEmbeddingModel:
    def test_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        cfg = LLMConfig(llm_api_key=None, llm_embedding_model="text-embedding-3-small")
        with pytest.raises(LLMConfigurationError, match="No API key"):
            build_embedding_model(cfg)

    def test_requires_embedding_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_EMBEDDING_MODEL", raising=False)
        cfg = LLMConfig(llm_api_key="test-key-123", llm_embedding_model=None)
        with pytest.raises(LLMConfigurationError, match="No embedding model"):
            build_embedding_model(cfg)

    def test_returns_openai_embeddings(self) -> None:
        cfg = LLMConfig(llm_api_key="test-key-123", llm_embedding_model="fake-embed")
        model = build_embedding_model(cfg)
        assert isinstance(model, OpenAIEmbeddings)
        assert model.model == "fake-embed"

    def test_model_override_wins(self) -> None:
        cfg = LLMConfig(llm_api_key="test-key-123", llm_embedding_model="cfg-model")
        model = build_embedding_model(cfg, model="override-model")
        assert model.model == "override-model"

    def test_maps_parameters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 显式置空 + 清环境变量：LLMConfig 会读 .env，本用例必须与用户配置隔离
        monkeypatch.delenv("LLM_EMBEDDING_DIMENSIONS", raising=False)
        cfg = LLMConfig(
            llm_api_key="test-key-123",
            llm_base_url="https://custom.api/v1",
            llm_timeout_seconds=60,
            llm_embedding_model="fake-embed",
            llm_embedding_dimensions=None,
        )
        model = build_embedding_model(cfg)

        assert model.request_timeout == 60
        assert "custom.api" in str(model.openai_api_base)
        # 第三方 OpenAI 兼容网关必须关闭本地 tiktoken 分词，直接发原文
        assert model.check_embedding_ctx_length is False
        # 未配置 dimensions 时不下发该参数
        assert model.dimensions is None

    def test_dimensions_passthrough(self) -> None:
        cfg = LLMConfig(
            llm_api_key="test-key-123",
            llm_embedding_model="fake-embed",
            llm_embedding_dimensions=1536,
        )
        model = build_embedding_model(cfg)
        assert model.dimensions == 1536
