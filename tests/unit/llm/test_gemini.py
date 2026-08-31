"""Tests for the Gemini Developer API gateway mapping."""

from __future__ import annotations

import json
from collections.abc import Mapping
from io import BytesIO
from typing import Any
from urllib.error import HTTPError

import pytest

from app.llm import (
    GeminiLLMGateway,
    LLMMessage,
    LLMMessageRole,
    LLMRequest,
    LLMResponse,
    NonRetryableLLMError,
    ToolCall,
    TransientLLMError,
)
from app.schemas.tools import ToolDefinition


class FakeGeminiTransport:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self._response = response
        self.calls: list[tuple[str, Mapping[str, str], dict[str, Any], float]] = []

    async def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append((url, headers, payload, timeout_seconds))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="search_mail",
        description="Search archived mail.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        result_schema={"type": "object"},
    )


def _gateway(transport: FakeGeminiTransport) -> GeminiLLMGateway:
    return GeminiLLMGateway(
        api_key="test-gemini-key",
        model="gemini-2.5-flash",
        transport=transport,
    )


async def test_gemini_gateway_maps_system_tools_and_function_calls() -> None:
    transport = FakeGeminiTransport(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "I will search."},
                            {
                                "functionCall": {
                                    "id": "call_1",
                                    "name": "search_mail",
                                    "args": {"query": "invoice"},
                                }
                            },
                        ],
                    }
                }
            ]
        }
    )

    response = await _gateway(transport).generate(
        LLMRequest(
            messages=[
                LLMMessage(role=LLMMessageRole.SYSTEM, content="Be concise."),
                LLMMessage(role=LLMMessageRole.USER, content="Find my invoice."),
            ],
            tools=[_tool()],
        )
    )

    assert response == LLMResponse(
        text="I will search.",
        tool_calls=[ToolCall(id="call_1", name="search_mail", arguments={"query": "invoice"})],
    )
    url, headers, payload, timeout_seconds = transport.calls[0]
    assert url.endswith("/models/gemini-2.5-flash:generateContent")
    assert headers["x-goog-api-key"] == "test-gemini-key"
    assert timeout_seconds == 30.0
    assert payload == {
        "systemInstruction": {"parts": [{"text": "Be concise."}]},
        "contents": [{"role": "user", "parts": [{"text": "Find my invoice."}]}],
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "search_mail",
                        "description": "Search archived mail.",
                        "parametersJsonSchema": _tool().parameters,
                    }
                ]
            }
        ],
    }


async def test_gemini_gateway_groups_tool_results_into_one_function_response_turn() -> None:
    transport = FakeGeminiTransport(
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Found it."}]}}]}
    )
    first_response = LLMResponse(
        tool_calls=[
            ToolCall(id="call_1", name="search_mail", arguments={"query": "invoice"}),
            ToolCall(id="call_2", name="search_mail", arguments={"query": "receipt"}),
        ]
    )

    response = await _gateway(transport).generate(
        LLMRequest(
            messages=[
                LLMMessage(role=LLMMessageRole.USER, content="Find my records."),
                first_response.as_assistant_message(),
                LLMMessage(
                    role=LLMMessageRole.TOOL,
                    content='{"tool_name":"search_mail","ok":true,"result":{"count":1}}',
                    tool_call_id="call_1",
                    tool_name="search_mail",
                ),
                LLMMessage(
                    role=LLMMessageRole.TOOL,
                    content='{"tool_name":"search_mail","ok":true,"result":{"count":2}}',
                    tool_call_id="call_2",
                    tool_name="search_mail",
                ),
            ],
            tools=[_tool()],
        )
    )

    assert response.text == "Found it."
    contents = transport.calls[0][2]["contents"]
    assert contents == [
        {"role": "user", "parts": [{"text": "Find my records."}]},
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {
                        "id": "call_1",
                        "name": "search_mail",
                        "args": {"query": "invoice"},
                    }
                },
                {
                    "functionCall": {
                        "id": "call_2",
                        "name": "search_mail",
                        "args": {"query": "receipt"},
                    }
                },
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_1",
                        "name": "search_mail",
                        "response": {
                            "tool_name": "search_mail",
                            "ok": True,
                            "result": {"count": 1},
                        },
                    }
                },
                {
                    "functionResponse": {
                        "id": "call_2",
                        "name": "search_mail",
                        "response": {
                            "tool_name": "search_mail",
                            "ok": True,
                            "result": {"count": 2},
                        },
                    }
                },
            ],
        },
    ]


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [(429, TransientLLMError), (503, TransientLLMError), (401, NonRetryableLLMError)],
)
async def test_gemini_gateway_classifies_provider_http_errors(
    status_code: int,
    error_type: type[Exception],
) -> None:
    transport = FakeGeminiTransport(
        HTTPError("https://example.invalid", status_code, "failure", hdrs=None, fp=None)
    )

    with pytest.raises(error_type):
        await _gateway(transport).generate(
            LLMRequest(messages=[LLMMessage(role=LLMMessageRole.USER, content="Hello")])
        )


async def test_gemini_gateway_rejects_malformed_provider_response() -> None:
    transport = FakeGeminiTransport({"candidates": []})

    with pytest.raises(NonRetryableLLMError, match="invalid response"):
        await _gateway(transport).generate(
            LLMRequest(messages=[LLMMessage(role=LLMMessageRole.USER, content="Hello")])
        )


async def test_gemini_gateway_includes_a_redacted_provider_error_summary() -> None:
    provider_error = {
        "error": {
            "message": "models/gemini-2.5-flash is unavailable; key=AQ.secret-value",
        }
    }
    transport = FakeGeminiTransport(
        HTTPError(
            "https://example.invalid",
            404,
            "not found",
            hdrs=None,
            fp=BytesIO(json.dumps(provider_error).encode("utf-8")),
        )
    )

    with pytest.raises(NonRetryableLLMError) as exc_info:
        await _gateway(transport).generate(
            LLMRequest(messages=[LLMMessage(role=LLMMessageRole.USER, content="Hello")])
        )

    message = str(exc_info.value)
    assert "models/gemini-2.5-flash is unavailable" in message
    assert "AQ.secret-value" not in message
    assert "[redacted]" in message


def test_gemini_gateway_requires_a_nonempty_key() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiLLMGateway(api_key="")
