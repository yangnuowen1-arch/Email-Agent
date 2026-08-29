"""单邮件意向分析 LangGraph：analyze → END。

从 coordinator 拿到清洗后的正文后进入本图，完成：
结构化 LLM 意向分析。

构建方式：
    graph = build_email_analysis_graph(chat_model)
    result = await graph.ainvoke(initial_state)

调用链追踪（invoke 结束后一次性输出全过程）：
    handler = GraphTraceHandler()
    result = await graph.ainvoke(initial_state, config={"callbacks": [handler]})
    logger.info("graph_trace", events=handler.dump())

错误处理：LLM 调用失败统一抛 ``LLMInvocationError``，原样穿透 ``ainvoke``
（LangGraph 不包装节点异常，异常链保留在 ``__cause__``），由调用方
``except AnalysisGraphError`` 捕获；节点其余逻辑错误不捕获、原样传播。
LLMInvocationError 由节点级 RetryPolicy 自动重试一次。

依赖全部闭包注入，无模块级全局，不触碰 DB。
"""

from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Any, NoReturn, Optional, TypedDict
from uuid import UUID

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy
from pydantic import ValidationError

from app.agent.errors import LLMInvocationError
from app.agent.prompts import EMAIL_ANALYSIS_SYSTEM_PROMPT
from app.schemas.analysis import EmailAnalysisOutput
from app.services.preprocess import compose_email_view

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 状态定义
# ---------------------------------------------------------------------------


class EmailAnalysisState(TypedDict, total=False):
    """单邮件分析节点间传递的状态。"""

    email_id: int
    account_id: int
    subject: str
    sender: str | None
    sent_at: Any  # datetime | None，避免 import 循环

    # coordinator 清洗后传入
    cleaned_text: str

    # analyze 节点产出
    primary_intent: str
    intents: list[dict]
    reasoning_summary: str
    entities: dict
    sentiment: str
    priority: str
    suggested_tools: list[str]
    llm_model: str


# ---------------------------------------------------------------------------
# 调用链追踪（langchain callbacks）
# ---------------------------------------------------------------------------


class GraphTraceHandler(BaseCallbackHandler):
    """收集一次图运行的链路事件，invoke 结束后由调用方 ``dump()`` 一次性输出。

    每次 invoke 应新建实例（随 config 传递，无全局状态）。事件按发生顺序记录，
    start 与 end 通过 run_id 关联计算耗时；LLM 事件需节点把 config 传给内层
    runnable 才能捕获（见 ``_analyze_node``）。
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._started_at: dict[UUID, float] = {}

    def _elapsed_ms(self, run_id: UUID) -> float:
        started = self._started_at.pop(run_id, time.monotonic())
        return round((time.monotonic() - started) * 1000, 1)

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
        self._events.append(
            {
                "type": "chain_start",
                "name": name,
                "run_id": str(run_id),
                "parent_run_id": self._parent(parent_run_id),
            }
        )

    def on_chain_end(self, outputs: dict[str, Any], *, run_id: UUID, **kwargs: Any) -> None:
        self._events.append(
            {"type": "chain_end", "run_id": str(run_id), "elapsed_ms": self._elapsed_ms(run_id)}
        )

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._events.append(
            {
                "type": "chain_error",
                "run_id": str(run_id),
                "elapsed_ms": self._elapsed_ms(run_id),
                "error": f"{type(error).__name__}: {error}",
            }
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._started_at[run_id] = time.monotonic()
        self._events.append(
            {
                "type": "llm_start",
                "name": name,
                "run_id": str(run_id),
                "parent_run_id": self._parent(parent_run_id),
            }
        )

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        usage = getattr(response, "usage_metadata", None)
        self._events.append(
            {
                "type": "llm_end",
                "run_id": str(run_id),
                "elapsed_ms": self._elapsed_ms(run_id),
                "usage": dict(usage) if usage else None,
            }
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._events.append(
            {
                "type": "llm_error",
                "run_id": str(run_id),
                "elapsed_ms": self._elapsed_ms(run_id),
                "error": f"{type(error).__name__}: {error}",
            }
        )

    def dump(self) -> list[dict[str, Any]]:
        """返回本次运行收集的全部事件（按发生顺序）。"""
        return list(self._events)


async def _analyze_node(
    state: EmailAnalysisState,
    # langgraph 按注解字面匹配来注入 config，仅认 RunnableConfig/Optional[RunnableConfig]，
    # 不识别 PEP 604 的 `X | None`（本文件启用 future annotations 后注解为字符串，须用 Optional）
    config: Optional[RunnableConfig] = None,  # noqa: UP045
    *,
    chat_model: Any,
    max_body_chars: int = 6000,
) -> dict:
    """调用 LLM 执行结构化意向分析。

    langgraph 会把运行时 config 注入到名为 ``config`` 的参数中；
    节点内必须把 config 继续传给内层 runnable，LLM 调用才会进入调用链追踪。

    LLM 调用失败（超时 / 网络 / 解析失败等）统一抛 ``LLMInvocationError``，
    由节点级 RetryPolicy 重试一次、重试耗尽后穿透 ``ainvoke``；
    节点其余逻辑（清洗视图组装等）的错误不捕获，原样传播以暴露代码 bug。
    """
    email_id = state.get("email_id", "?")

    cleaned = state.get("cleaned_text", "")
    view = compose_email_view(
        subject=state.get("subject", ""),
        sender=state.get("sender"),
        sent_at=state.get("sent_at"),
        cleaned_text=cleaned[:max_body_chars],
    )

    logger.debug(
        "llm_analysis_start",
        email_id=email_id,
        subject=state.get("subject", "")[:100],
        prompt_length=len(view),
    )

    t0 = time.monotonic()

    def _fail(message: str, exc: Exception) -> NoReturn:
        """记录失败日志后以类型化异常上抛，原始异常保留在 __cause__。"""
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.error(
            "llm_analysis_failed",
            email_id=email_id,
            elapsed_ms=round(elapsed_ms, 1),
            error_type=type(exc).__name__,
            error_message=message[:500],
            exc_info=True,
        )
        raise LLMInvocationError(message) from exc

    try:
        structured = chat_model.with_structured_output(
            EmailAnalysisOutput, method="function_calling"
        )
        result = await asyncio.wait_for(
            structured.ainvoke(
                [
                    SystemMessage(content=EMAIL_ANALYSIS_SYSTEM_PROMPT),
                    HumanMessage(content=view),
                ],
                config=config,
            ),
            timeout=120,
        )
    except asyncio.TimeoutError as exc:
        _fail("LLM 调用超时（120s）", exc)
    except (OutputParserException, ValidationError) as exc:
        _fail(f"LLM 输出解析失败: {exc}", exc)
    except Exception as exc:  # noqa: BLE001
        _fail(f"{type(exc).__name__}: {exc}", exc)

    if result is None:
        logger.error("llm_analysis_returned_none", email_id=email_id)
        raise LLMInvocationError("LLM returned None")

    elapsed_ms = (time.monotonic() - t0) * 1000
    usage = getattr(result, "response_metadata", {}).get("token_usage", {})

    logger.info(
        "llm_analysis_done",
        email_id=email_id,
        model=getattr(chat_model, "model_name", "unknown"),
        elapsed_ms=round(elapsed_ms, 1),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )

    return {
        "primary_intent": result.primary_intent,
        "intents": [i.model_dump() for i in result.intents],
        "reasoning_summary": result.reasoning_summary,
        "entities": result.entities,
        "sentiment": result.sentiment,
        "priority": result.priority,
        "suggested_tools": result.suggested_tools,
        "llm_model": getattr(chat_model, "model_name", "unknown"),
    }


def build_email_analysis_graph(chat_model: Any):
    """构建单邮件意向分析图，依赖全部闭包注入。

    调用链追踪：ainvoke 时传 ``config={"callbacks": [GraphTraceHandler()]}``，
    结束后由 ``handler.dump()`` 取事件列表（用法见模块 docstring）。

    analyze 节点挂 RetryPolicy：仅对 LLMInvocationError 重试一次
    （max_attempts=2 含首次），重试耗尽后异常原样抛给 ainvoke 调用方。
    """

    builder = StateGraph(EmailAnalysisState)

    builder.add_node(
        "analyze",
        partial(_analyze_node, chat_model=chat_model),
        retry_policy=RetryPolicy(max_attempts=2, retry_on=(LLMInvocationError,)),
    )

    builder.add_edge("analyze", END)

    builder.set_entry_point("analyze")

    return builder.compile()
