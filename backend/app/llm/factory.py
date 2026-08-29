"""LLM 工厂：按配置构建 langchain-openai ChatOpenAI 实例。"""

from __future__ import annotations

import httpx
import structlog
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_openai import ChatOpenAI

from app.core.settings import LLMConfig
from app.llm.errors import LLMConfigurationError

logger = structlog.get_logger(__name__)


async def _log_request(request: httpx.Request) -> None:
    logger.debug(
        "llm_request",
        method=request.method,
        url=str(request.url),
        body_size=len(request.content),
    )


async def _log_response(response: httpx.Response) -> None:
    logger.debug(
        "llm_response",
        status=response.status_code

    )


async def _log_error(error: Exception) -> None:
    logger.error(
        "llm_connection_error",
        error_type=type(error).__name__,
        error_message=str(error),
    )


def build_chat_model(llm_config: LLMConfig) -> ChatOpenAI:
    """按 LLMConfig 构建 ChatOpenAI，兼容任意 OpenAI 兼容端点。

    Raises LLMConfigurationError when no API key is configured.
    """
    if not llm_config.llm_api_key:
        raise LLMConfigurationError(
            f"No API key configured for LLM provider '{llm_config.llm_provider}'"
        )

    async_client = httpx.AsyncClient(
        event_hooks={
            "request": [_log_request],
            "response": [_log_response],
        },
        timeout=httpx.Timeout(
            connect=10.0,
            read=float(llm_config.llm_timeout_seconds),
            write=10.0,
            pool=10.0,
        ),
    )

    # 1. 配置速率限制器：每10秒最多发起1次请求
    rate_limiter = InMemoryRateLimiter(
        requests_per_second=1,  # 0.1次/秒 = 1次/10秒
        check_every_n_seconds=0.1,
        max_bucket_size=1,  # 不允许突发请求
    )

    return ChatOpenAI(
        model=llm_config.llm_model,
        api_key=llm_config.llm_api_key,
        base_url=llm_config.llm_base_url,
        temperature=llm_config.llm_temperature,
        max_tokens=llm_config.llm_max_tokens,
        timeout=llm_config.llm_timeout_seconds,
        max_retries=3,
        http_async_client=async_client,
        rate_limiter=rate_limiter,
    )
