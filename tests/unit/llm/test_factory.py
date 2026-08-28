"""LLM factory 单元测试：验证 build_chat_model 参数映射与 API key 校验。"""

from __future__ import annotations

import pytest
from langchain_openai import ChatOpenAI

from app.core.settings import LLMConfig
from app.llm.errors import LLMConfigurationError
from app.llm.factory import build_chat_model


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
