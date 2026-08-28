"""LLM 工厂：按配置构建 langchain-openai ChatOpenAI 实例。"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.core.settings import LLMConfig
from app.llm.errors import LLMConfigurationError


def build_chat_model(llm_config: LLMConfig) -> ChatOpenAI:
    """按 LLMConfig 构建 ChatOpenAI，兼容任意 OpenAI 兼容端点。

    Raises LLMConfigurationError when no API key is configured.
    """
    if not llm_config.llm_api_key:
        raise LLMConfigurationError(
            f"No API key configured for LLM provider '{llm_config.llm_provider}'"
        )
    return ChatOpenAI(
        model=llm_config.llm_model,
        api_key=llm_config.llm_api_key,
        base_url=llm_config.llm_base_url,
        temperature=llm_config.llm_temperature,
        max_tokens=llm_config.llm_max_tokens,
        timeout=llm_config.llm_timeout_seconds,
        max_retries=3,
    )
