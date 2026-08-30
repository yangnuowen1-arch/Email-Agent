"""LangGraph / LangChain 调用链追踪 handler：边收集事件边输出结构化日志。

``GraphTraceHandler`` 挂在 ``ainvoke`` 的 ``config={"callbacks": [...]}`` 上，
把链路事件（节点 / LLM 的开始、耗时、token usage、错误）以 structlog 日志
实时输出，同时收集在内部供 ``dump()`` 事后取用（测试 / 程序化分析）、
供 ``usage_summary()`` 聚合本次运行的 token 消耗（analyze / 翻译 / 草稿
全部 LLM 调用求和）：

    handler = GraphTraceHandler()
    result = await graph.ainvoke(initial_state, config={"callbacks": [handler]})

email_id / account_id 等业务上下文不经 handler 传递：由调用方
``structlog.contextvars.bind_contextvars`` 绑定，经 merge_contextvars
处理器自动合并进本 handler 输出的每条日志（LangChain 的 metadata 只
透传给 Start 事件，End/Error 回调拿不到，故不采用）。

每次 invoke 应新建实例（随 config 传递，无全局状态）。
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

import structlog
from langchain_core.callbacks import BaseCallbackHandler

logger = structlog.get_logger(__name__)


def _prompt_chars(messages: list[list[Any]]) -> int:
    """估算 prompt 字符数（``on_chat_model_start`` 收到 list[list[BaseMessage]]）。"""
    total = 0
    for batch in messages:
        for message in batch:
            content = getattr(message, "content", "")
            total += len(content) if isinstance(content, str) else len(str(content))
    return total


class GraphTraceHandler(BaseCallbackHandler):
    """收集一次图运行的链路事件并实时输出结构化日志，``dump()`` 可事后取事件列表。"""

    def __init__(self, *, emit_logs: bool = True) -> None:
        self._emit_logs = emit_logs
        self._events: list[dict[str, Any]] = []
        self._started_at: dict[UUID, float] = {}
        # End/Error 回调签名里没有 name/metadata（langchain-core 只在 Start 事件透传），
        # 在 start 时记录，供同名 run 的结束 / 错误日志取用
        self._names: dict[UUID, str | None] = {}

    def _elapsed_ms(self, run_id: UUID) -> float:
        started = self._started_at.pop(run_id, time.monotonic())
        return round((time.monotonic() - started) * 1000, 1)

    def _name(self, run_id: UUID) -> str | None:
        return self._names.pop(run_id, None)

    def _parent(self, parent_run_id: UUID | None) -> str | None:
        return str(parent_run_id) if parent_run_id else None

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._started_at[run_id] = time.monotonic()
        self._names[run_id] = name
        self._events.append(
            {
                "type": "chain_start",
                "name": name,
                "run_id": str(run_id),
                "parent_run_id": self._parent(parent_run_id),
            }
        )
        if self._emit_logs:
            logger.debug("graph_chain_start", name=name)

    def on_chain_end(self, outputs: dict[str, Any], *, run_id: UUID, **kwargs: Any) -> None:
        elapsed_ms = self._elapsed_ms(run_id)
        name = self._name(run_id)
        self._events.append(
            {"type": "chain_end", "run_id": str(run_id), "elapsed_ms": elapsed_ms}
        )
        if self._emit_logs:
            logger.debug("graph_chain_end", name=name, elapsed_ms=elapsed_ms)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        elapsed_ms = self._elapsed_ms(run_id)
        name = self._name(run_id)
        self._events.append(
            {
                "type": "chain_error",
                "run_id": str(run_id),
                "elapsed_ms": elapsed_ms,
                "error": f"{type(error).__name__}: {error}",
            }
        )
        if self._emit_logs:
            logger.error(
                "graph_chain_error", name=name, elapsed_ms=elapsed_ms, exc_info=error
            )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._started_at[run_id] = time.monotonic()
        self._names[run_id] = name
        self._events.append(
            {
                "type": "llm_start",
                "name": name,
                "run_id": str(run_id),
                "parent_run_id": self._parent(parent_run_id),
            }
        )
        if self._emit_logs:
            logger.debug(
                "llm_call_start", model=name, prompt_chars=_prompt_chars(messages)
            )

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        elapsed_ms = self._elapsed_ms(run_id)
        usage = getattr(response, "usage_metadata", None)
        usage_dict = dict(usage) if usage else None
        self._events.append(
            {
                "type": "llm_end",
                "run_id": str(run_id),
                "elapsed_ms": elapsed_ms,
                "usage": usage_dict,
            }
        )
        if self._emit_logs:
            logger.info("llm_call_done", elapsed_ms=elapsed_ms, usage=usage_dict)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        elapsed_ms = self._elapsed_ms(run_id)
        self._events.append(
            {
                "type": "llm_error",
                "run_id": str(run_id),
                "elapsed_ms": elapsed_ms,
                "error": f"{type(error).__name__}: {error}",
            }
        )
        if self._emit_logs:
            logger.error("llm_call_failed", elapsed_ms=elapsed_ms, exc_info=error)

    def dump(self) -> list[dict[str, Any]]:
        """返回本次运行收集的全部事件（按发生顺序）。"""
        return list(self._events)

    def usage_summary(self) -> dict[str, int] | None:
        """聚合本次运行的 LLM token 消耗；无 LLM 调用（含全失败）返回 None。

        一次 ainvoke 的 callbacks 覆盖图内全部 LLM 调用（analyze / 翻译 / 草稿），
        汇总即单封邮件的总消耗；单条 usage 来自 ``on_llm_end`` 的 usage_metadata，
        失败调用（llm_error）无 usage 不计入。
        """
        totals = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for event in self._events:
            if event["type"] != "llm_end":
                continue
            usage = event.get("usage") or {}
            totals["llm_calls"] += 1
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                totals[key] += int(usage.get(key) or 0)
        return totals if totals["llm_calls"] else None
