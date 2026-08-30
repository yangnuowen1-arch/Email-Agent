"""LLM 工厂：按配置构建 langchain-openai ChatOpenAI / OpenAIEmbeddings 实例。"""

from __future__ import annotations

import httpx
import structlog
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

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
    logger.debug("llm_response", status=response.status_code)


async def _log_error(error: Exception) -> None:
    logger.error(
        "llm_connection_error",
        error_type=type(error).__name__,
        error_message=str(error),
    )


def build_chat_model(llm_config: LLMConfig, *, model: str | None = None) -> ChatOpenAI:
    """按 LLMConfig 构建 ChatOpenAI，兼容任意 OpenAI 兼容端点。

    ``model`` 用于覆盖配置中的默认模型名（如构建视觉模型客户端）；
    为 None 时使用 ``llm_config.llm_model``。

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
        model=model or llm_config.llm_model,
        api_key=llm_config.llm_api_key,
        base_url=llm_config.llm_base_url,
        temperature=llm_config.llm_temperature,
        max_tokens=llm_config.llm_max_tokens,
        timeout=llm_config.llm_timeout_seconds,
        max_retries=3,
        http_async_client=async_client,
        rate_limiter=rate_limiter,
    )


def build_embedding_model(llm_config: LLMConfig, *, model: str | None = None) -> OpenAIEmbeddings:
    """按 LLMConfig 构建 OpenAIEmbeddings，走同网关的 /embeddings 端点。

    ``model`` 用于覆盖配置中的 embedding 模型名；为 None 时使用
    ``llm_config.llm_embedding_model``。

    Raises LLMConfigurationError when no API key or no embedding model is configured.
    """
    if not llm_config.llm_api_key:
        raise LLMConfigurationError(
            f"No API key configured for LLM provider '{llm_config.llm_provider}'"
        )
    embedding_model = model or llm_config.llm_embedding_model
    if not embedding_model:
        msg = (
            "No embedding model configured; set LLM_EMBEDDING_MODEL in environment "
            "or .env (see .env.example)"
        )
        raise LLMConfigurationError(msg)

    # check_embedding_ctx_length=False：第三方 OpenAI 兼容网关普遍不认本地
    # tiktoken 分词出的 token 数组，关掉后直接发送原文，由网关自行处理长度
    return OpenAIEmbeddings(
        model=embedding_model,
        api_key=llm_config.llm_api_key,
        base_url=llm_config.llm_base_url,
        timeout=llm_config.llm_timeout_seconds,
        max_retries=3,
        dimensions=llm_config.llm_embedding_dimensions,
        check_embedding_ctx_length=False,
    )
