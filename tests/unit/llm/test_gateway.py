"""Unit tests for the OpenAI gateway using a mocked AsyncOpenAI client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from app.core.settings import LLMConfig
from app.llm.base import ChatResponse, LLMConfigurationError
from app.llm.gateway import (
    OpenAIGateway,
    _message_payload,
    _tool_payload,
    build_llm_gateway,
)


@tool("send_email", description="Send an email to a recipient")
def send_email(to: str) -> str:
    """Send an email."""
    return "sent"


def _make_completion(
    content: str = "hi",
    tool_calls: list | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 1,
    completion_tokens: int = 2,
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class _FakeCompletions:
    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    async def create(self, **kwargs) -> object:
        self.last_kwargs = kwargs
        if kwargs.get("stream"):

            async def gen():
                for piece in ["Hel", "lo"]:
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))]
                    )

            return gen()
        if kwargs.get("response_format", {}).get("type") == "json_object":
            return _make_completion(content=json.dumps({"ok": True}))
        return _make_completion(
            content="hi",
            tool_calls=[
                SimpleNamespace(id="c1", function=SimpleNamespace(name="send", arguments='{"to":"a"}'))
            ],
            finish_reason="tool_calls",
            prompt_tokens=3,
            completion_tokens=4,
        )


class _FakeClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions())
        self.api_key = "k"
        self.base_url = "http://x"


@pytest.fixture
def fake_factory(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"clients": []}
    monkeypatch.setattr(
        "app.llm.gateway.AsyncOpenAI",
        lambda *a, **k: state["clients"].append(_FakeClient()) or state["clients"][-1],
    )
    return state


def _gw() -> OpenAIGateway:
    return OpenAIGateway(provider="p", model="m", api_key="k", base_url="http://x")


# --- payload conversion (pure, no network) ---------------------------------


def test_message_payload_system_user_tool() -> None:
    assert _message_payload(SystemMessage(content="s")) == {"role": "system", "content": "s"}
    assert _message_payload(HumanMessage(content="h")) == {"role": "user", "content": "h"}
    assert _message_payload(ToolMessage(content="r", tool_call_id="t1")) == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": "r",
    }


def test_message_payload_assistant_with_tool_calls() -> None:
    message = AIMessage(content="", tool_calls=[{"id": "c1", "name": "send", "args": {"to": "a"}}])
    payload = _message_payload(message)
    assert payload["role"] == "assistant"
    assert payload["tool_calls"][0]["function"]["name"] == "send"
    assert json.loads(payload["tool_calls"][0]["function"]["arguments"]) == {"to": "a"}


def test_tool_payload() -> None:
    payload = _tool_payload(send_email)
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "send_email"
    assert "to" in payload["function"]["parameters"]["properties"]


# --- gateway construction ---------------------------------------------------


def test_openai_gateway_requires_api_key() -> None:
    with pytest.raises(LLMConfigurationError):
        OpenAIGateway(provider="p", model="m", api_key="", base_url="http://x")


# --- chat / stream / structured_output (mocked) ----------------------------


async def test_chat_maps_response(fake_factory: dict) -> None:
    gw = _gw()
    resp = await gw.chat([HumanMessage(content="hi")])

    assert isinstance(resp, ChatResponse)
    assert resp.content == "hi"
    assert resp.tool_calls[0].name == "send"
    assert resp.tool_calls[0].arguments == {"to": "a"}
    assert resp.prompt_tokens == 3
    assert resp.completion_tokens == 4
    assert resp.finish_reason == "tool_calls"

    last = fake_factory["clients"][-1].chat.completions.last_kwargs
    assert last["model"] == "m"
    assert last["messages"] == [{"role": "user", "content": "hi"}]


async def test_stream_yields_chunks(fake_factory: dict) -> None:
    gw = _gw()
    chunks = [chunk async for chunk in gw.stream([HumanMessage(content="hi")])]
    assert chunks == ["Hel", "lo"]

    last = fake_factory["clients"][-1].chat.completions.last_kwargs
    assert last["stream"] is True


async def test_bind_tools_injects_tools(fake_factory: dict) -> None:
    gw = _gw().bind_tools([send_email])
    await gw.chat([HumanMessage(content="hi")])

    last = fake_factory["clients"][-1].chat.completions.last_kwargs
    assert last["tools"][0]["function"]["name"] == "send_email"
    assert last["tool_choice"] == "auto"


async def test_structured_output_parses_schema(fake_factory: dict) -> None:
    class Result(BaseModel):
        ok: bool

    gw = _gw()
    result = await gw.structured_output([HumanMessage(content="go")], Result)
    assert result.ok is True

    last = fake_factory["clients"][-1].chat.completions.last_kwargs
    assert last["response_format"] == {"type": "json_object"}


# --- factory ----------------------------------------------------------------


def test_build_llm_gateway_requires_key() -> None:
    with pytest.raises(LLMConfigurationError):
        build_llm_gateway(LLMConfig(llm_api_key=None))


def test_build_llm_gateway_returns_gateway() -> None:
    gw = build_llm_gateway(LLMConfig(llm_api_key="k"))
    assert isinstance(gw, OpenAIGateway)
