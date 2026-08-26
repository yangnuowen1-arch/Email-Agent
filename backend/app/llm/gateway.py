"""OpenAI SDK backed concrete gateway implementing :class:`LLMGateway`.

Mirrors the request/response shaping of GoldAgent's ``OpenAICompatibleGateway``
but delegates HTTP and retries to the official ``openai`` SDK instead of raw
httpx, so no extra networking/retry dependencies are required.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, TypeVar

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from openai import APITimeoutError, AsyncOpenAI, OpenAIError
from pydantic import BaseModel

from app.core.settings import LLMConfig
from app.llm.base import (
    ChatResponse,
    LLMConfigurationError,
    LLMGateway,
    LLMProviderError,
    ToolCall,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _message_payload(message: BaseMessage) -> dict[str, Any]:
    """Convert a langchain message into an OpenAI chat completion payload."""
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": str(message.content)}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": str(message.content)}
    if isinstance(message, ToolMessage):
        payload: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": str(message.content),
        }
        if message.name:
            payload["name"] = message.name
        return payload
    if isinstance(message, AIMessage):
        payload = {"role": "assistant", "content": str(message.content or "")}
        raw_calls = message.tool_calls or []
        if raw_calls:
            payload["tool_calls"] = [
                {
                    "id": call.get("id") or str(uuid.uuid4()),
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("args", {}), ensure_ascii=False),
                    },
                }
                for call in raw_calls
            ]
        return payload
    return {"role": "user", "content": str(message.content)}


def _tool_payload(tool: BaseTool) -> dict[str, Any]:
    """Convert a langchain tool into an OpenAI function-tool payload."""
    if tool.args_schema:
        schema = tool.args_schema.model_json_schema()
    else:
        schema = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": schema},
    }


class OpenAIGateway(LLMGateway):
    """Concrete gateway talking to any OpenAI-compatible ``/chat/completions`` API."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
        bound_tools: list[BaseTool] | None = None,
    ) -> None:
        if not api_key:
            raise LLMConfigurationError(f"API key for provider '{provider}' is not configured")
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.bound_tools = bound_tools
        self.last_latency_ms: float | None = None
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            max_retries=3,
        )

    def bind_tools(self, tools: list[BaseTool]) -> OpenAIGateway:
        """Return a copy of this gateway with ``tools`` pre-bound for every call."""
        return OpenAIGateway(
            provider=self.provider,
            model=self.model,
            api_key=self._client.api_key,
            base_url=str(self._client.base_url),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout_seconds=self.timeout_seconds,
            bound_tools=tools,
        )

    def _payload(
        self, messages: list[BaseMessage], tools: list[BaseTool] | None, stream: bool = False
    ) -> dict[str, Any]:
        selected_tools = tools if tools is not None else self.bound_tools
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_payload(message) for message in messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if selected_tools:
            payload["tools"] = [_tool_payload(tool) for tool in selected_tools]
            payload["tool_choice"] = "auto"
        return payload

    async def chat(
        self, messages: list[BaseMessage], tools: list[BaseTool] | None = None
    ) -> ChatResponse:
        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(**self._payload(messages, tools))
        except APITimeoutError as exc:
            raise LLMProviderError(
                self.provider,
                f"LLM request exceeded the {self.timeout_seconds}s total timeout",
            ) from exc
        except OpenAIError as exc:
            raise LLMProviderError(
                self.provider, f"LLM request failed: {type(exc).__name__}"
            ) from exc
        finally:
            self.last_latency_ms = (time.perf_counter() - started) * 1000

        choice = response.choices[0]
        message = choice.message
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            function = call.function
            raw_arguments = function.arguments
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else raw_arguments
                )
            except json.JSONDecodeError:
                arguments = {}
            calls.append(
                ToolCall(
                    id=call.id or str(uuid.uuid4()),
                    name=function.name or "",
                    arguments=arguments,
                )
            )
        usage = response.usage
        return ChatResponse(
            content=str(message.content or ""),
            tool_calls=calls,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            finish_reason=choice.finish_reason,
        )

    async def stream(
        self, messages: list[BaseMessage], tools: list[BaseTool] | None = None
    ) -> AsyncIterator[str]:
        try:
            stream = await self._client.chat.completions.create(
                **self._payload(messages, tools, stream=True)
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield str(delta)
        except APITimeoutError as exc:
            raise LLMProviderError(
                self.provider,
                f"LLM stream exceeded the {self.timeout_seconds}s total timeout",
            ) from exc
        except OpenAIError as exc:
            raise LLMProviderError(
                self.provider, f"LLM stream failed: {type(exc).__name__}"
            ) from exc

    async def structured_output(
        self, messages: list[BaseMessage], schema: type[SchemaT]
    ) -> SchemaT:
        schema_instruction = SystemMessage(
            content=(
                "Return JSON only, matching this schema: "
                f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
            )
        )
        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    _message_payload(schema_instruction),
                    *[_message_payload(message) for message in messages],
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
            )
        except APITimeoutError as exc:
            raise LLMProviderError(
                self.provider,
                f"LLM request exceeded the {self.timeout_seconds}s total timeout",
            ) from exc
        except OpenAIError as exc:
            raise LLMProviderError(
                self.provider, f"LLM request failed: {type(exc).__name__}"
            ) from exc
        finally:
            self.last_latency_ms = (time.perf_counter() - started) * 1000

        raw = response.choices[0].message.content or ""
        cleaned = raw.strip().removeprefix("```json").removesuffix("```").strip()
        return schema.model_validate_json(cleaned)


def build_llm_gateway(llm_config: LLMConfig) -> OpenAIGateway:
    """Construct the OpenAI gateway from application settings.

    Raises :class:`LLMConfigurationError` when no API key is configured so the
    failure is explicit rather than surfacing as an opaque provider error later.
    """
    api_key = llm_config.llm_api_key
    if not api_key:
        raise LLMConfigurationError(
            f"No API key configured for LLM provider '{llm_config.llm_provider}'"
        )
    return OpenAIGateway(
        provider=llm_config.llm_provider,
        model=llm_config.llm_model,
        api_key=api_key,
        base_url=llm_config.llm_base_url,
        temperature=llm_config.llm_temperature,
        max_tokens=llm_config.llm_max_tokens,
        timeout_seconds=llm_config.llm_timeout_seconds,
    )
