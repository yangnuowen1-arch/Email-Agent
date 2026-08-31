"""Gemini Developer API adapter for the provider-neutral LLM gateway."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.llm.client import LLMMessage, LLMMessageRole, LLMRequest, LLMResponse, ToolCall
from app.llm.errors import NonRetryableLLMError, TransientLLMError

GeminiTransport = Callable[
    [str, Mapping[str, str], dict[str, Any], float],
    Awaitable[dict[str, Any]],
]

_DEFAULT_MODEL = "gemini-3.6-flash"
_API_URL_PREFIX = "https://generativelanguage.googleapis.com/v1beta/models/"
_MAX_PROVIDER_ERROR_MESSAGE_CHARS = 500
_API_KEY_PATTERN = re.compile(r"\b(?:AIza[A-Za-z0-9_-]+|AQ\.[A-Za-z0-9_-]+)\b")
_QUERY_KEY_PATTERN = re.compile(r"(?i)([?&]key=)[^&\s]+")


class GeminiLLMGateway:
    """Call the Gemini Developer API without exposing credentials to callers.

    The adapter intentionally uses the REST API directly, keeping the project
    independent of a provider SDK and making the provider boundary easy to
    exercise with an injected transport in unit tests.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        timeout_seconds: float = 30.0,
        transport: GeminiTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("GEMINI_API_KEY is required but missing or empty")
        if not isinstance(model, str) or not _is_model_name(model):
            raise ValueError("Gemini model must be a non-empty model ID")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("Gemini timeout_seconds must be positive")

        self._api_key = api_key.strip()
        self._model = model.strip()
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport or _post_gemini_request

    @property
    def model(self) -> str:
        """Return the configured model ID without revealing credentials."""

        return self._model

    async def complete(self, prompt: str) -> LLMResponse:
        """Support the legacy single-prompt completion interface."""

        return await self.generate(
            LLMRequest(messages=[LLMMessage(role=LLMMessageRole.USER, content=prompt)])
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate one text or function-calling turn through Gemini."""

        try:
            response = await self._transport(
                f"{_API_URL_PREFIX}{self._model}:generateContent",
                {
                    "accept": "application/json",
                    "content-type": "application/json",
                    "x-goog-api-key": self._api_key,
                },
                _request_payload(request),
                self._timeout_seconds,
            )
            return _response_from_payload(response)
        except (TransientLLMError, NonRetryableLLMError):
            raise
        except HTTPError as exc:
            detail = _provider_error_summary(exc)
            suffix = f": {detail}" if detail else ""
            if exc.code == 429 or 500 <= exc.code <= 599:
                raise TransientLLMError(
                    f"Gemini request received retryable HTTP status {exc.code}{suffix}"
                ) from exc
            raise NonRetryableLLMError(
                f"Gemini request received HTTP status {exc.code}{suffix}"
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise TransientLLMError("Gemini request could not reach the provider") from exc
        except (TypeError, ValueError) as exc:
            raise NonRetryableLLMError("Gemini returned an invalid response") from exc


async def _post_gemini_request(
    url: str,
    headers: Mapping[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Perform a blocking standard-library request outside the event loop."""

    return await asyncio.to_thread(
        _post_json,
        url,
        headers,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        timeout_seconds,
    )


def _post_json(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> dict[str, Any]:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    with urlopen(request, timeout=timeout_seconds) as http_response:  # noqa: S310 - fixed API host
        decoded = json.loads(http_response.read().decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Gemini response must be a JSON object")
    return decoded


def _provider_error_summary(error: HTTPError) -> str | None:
    """Extract a bounded, redacted Gemini error message for local diagnosis.

    Provider errors are never logged by the agent runtime. A direct caller can
    still see this short summary, which distinguishes an unavailable model from
    an API/project permission problem without exposing a request URL or key.
    """

    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (AttributeError, OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    provider_error = payload.get("error")
    if not isinstance(provider_error, dict):
        return None
    message = provider_error.get("message")
    if not isinstance(message, str):
        return None

    compact_message = " ".join(message.split())
    redacted_message = _API_KEY_PATTERN.sub("[redacted]", compact_message)
    redacted_message = _QUERY_KEY_PATTERN.sub(r"\1[redacted]", redacted_message)
    return redacted_message[:_MAX_PROVIDER_ERROR_MESSAGE_CHARS] or None


def _request_payload(request: LLMRequest) -> dict[str, Any]:
    """Translate the replayable local transcript into Gemini REST content."""

    system_text: list[str] = []
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    index = 0

    while index < len(request.messages):
        message = request.messages[index]
        if message.role is LLMMessageRole.SYSTEM:
            # Gemini accepts one system instruction; preserve every system
            # message in order rather than silently dropping later ones.
            system_text.append(_required_content(message))
            index += 1
            continue

        if message.role is LLMMessageRole.USER:
            contents.append({"role": "user", "parts": [{"text": _required_content(message)}]})
            index += 1
            continue

        if message.role is LLMMessageRole.ASSISTANT:
            parts: list[dict[str, Any]] = []
            if message.content is not None:
                parts.append({"text": message.content})
            for call in message.tool_calls:
                call_names[call.id] = call.name
                parts.append(
                    {
                        "functionCall": {
                            "id": call.id,
                            "name": call.name,
                            "args": call.arguments,
                        }
                    }
                )
            contents.append({"role": "model", "parts": parts})
            index += 1
            continue

        # Consecutive local tool observations must be returned together in one
        # Gemini user turn. This matters when a model issued multiple calls.
        tool_parts: list[dict[str, Any]] = []
        while index < len(request.messages):
            tool_message = request.messages[index]
            if tool_message.role is not LLMMessageRole.TOOL:
                break
            call_id = tool_message.tool_call_id
            tool_name = tool_message.tool_name or (call_names.get(call_id) if call_id else None)
            if call_id is None or tool_name is None:
                raise ValueError("tool message could not be matched to a Gemini function call")
            tool_parts.append(
                {
                    "functionResponse": {
                        "id": call_id,
                        "name": tool_name,
                        "response": json.loads(_required_content(tool_message)),
                    }
                }
            )
            index += 1
        contents.append({"role": "user", "parts": tool_parts})

    if not contents:
        raise ValueError("Gemini requires at least one user or model message")

    payload: dict[str, Any] = {"contents": contents}
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_text)}]}
    if request.tools:
        payload["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parametersJsonSchema": tool.parameters,
                    }
                    for tool in request.tools
                ]
            }
        ]
    return payload


def _response_from_payload(payload: object) -> LLMResponse:
    """Translate Gemini text/function-call parts into the local response."""

    if not isinstance(payload, dict):
        raise ValueError("Gemini response must be a JSON object")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gemini response did not include a candidate")
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise ValueError("Gemini candidate must be an object")
    content = candidate.get("content")
    if not isinstance(content, dict):
        raise ValueError("Gemini candidate did not include content")
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise ValueError("Gemini candidate content did not include parts")

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise ValueError("Gemini content part must be an object")
        text = part.get("text")
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("Gemini text part must be a string")
            text_parts.append(text)

        function_call = part.get("functionCall")
        if function_call is not None:
            tool_calls.append(_tool_call_from_payload(function_call, index))

    text = "\n".join(text_parts) if text_parts else None
    return LLMResponse(text=text, tool_calls=tool_calls)


def _tool_call_from_payload(payload: object, index: int) -> ToolCall:
    if not isinstance(payload, dict):
        raise ValueError("Gemini function call must be an object")
    name = payload.get("name")
    arguments = payload.get("args", {})
    call_id = payload.get("id") or f"gemini_call_{index}"
    if not isinstance(name, str) or not isinstance(arguments, dict) or not isinstance(call_id, str):
        raise ValueError("Gemini function call has invalid fields")
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _required_content(message: LLMMessage) -> str:
    if message.content is None:
        raise ValueError(f"{message.role.value} message requires content")
    return message.content


def _is_model_name(value: str) -> bool:
    candidate = value.strip()
    return (
        bool(candidate)
        and len(candidate) <= 128
        and all(
            (character.isascii() and character.isalnum()) or character in {".", "_", "-"}
            for character in candidate
        )
    )
