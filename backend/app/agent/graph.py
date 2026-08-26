"""LangGraph 编排的邮件智能体。

用最小可用的 ``StateGraph`` 承载一次「接收任务 → 调用 LLM → 返回结果」的流转，
LLM 网关通过构造参数注入，不在模块内持有全局单例。
"""

from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.agent.prompts import SYSTEM_PROMPT
from app.core import LLMConfig
from app.llm.base import ChatResponse, LLMGateway, ToolCall
from app.llm.gateway import build_llm_gateway


class AgentState(TypedDict, total=False):
    """LangGraph 在节点间传递的状态；messages 在节点间归并。"""

    messages: list[BaseMessage]
    tool_calls: list[ToolCall]


class EmailAgent:
    """基于 LangGraph 的邮件智能体门面。

    对外签名刻意与原 ``EmailAgent`` 保持一致：``(llm_config, gateway=None)``，
    入口 ``respond`` 返回 ``ChatResponse``。图结构固定为 ``START → respond → END``，
    ``respond`` 节点取出用户任务、拼接系统提示后委托注入的 ``LLMGateway`` 完成，
    再把回复与工具调用写回状态。
    """

    def __init__(
        self, llm_config: LLMConfig, gateway: LLMGateway | None = None
    ) -> None:
        # 未显式注入网关时，按配置构建一个真实网关；无 API key 会在构建时明确报错
        self._gateway = gateway or build_llm_gateway(llm_config)
        self._graph = self._build_graph()

    def _build_graph(self):
        """装配并编译状态图；节点与边在此声明，编译结果在构造时缓存一次。"""
        builder = StateGraph(AgentState)

        builder.add_node("respond", self._respond)
        builder.add_edge(START, "respond")
        builder.add_edge("respond", END)

        return builder.compile()

    async def _respond(self, state: AgentState) -> dict:
        """执行单步回答：取任务文本 → 调网关 → 以 AIMessage/工具调用回填状态。"""
        if self._gateway is None:
            raise RuntimeError("LLM 网关未注入：请在构造 EmailAgent 时提供 gateway")

        task = self._extract_task(state.get("messages", []))
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=task.strip()),
        ]
        response = await self._gateway.chat(messages)

        return {
            "messages": [AIMessage(content=response.content)],
            "tool_calls": response.tool_calls,
        }

    @staticmethod
    def _extract_task(messages: list[BaseMessage]) -> str:
        """从消息流中取最后一条用户消息作为任务文本，缺省回退空串。"""
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                return str(message.content)
        return ""

    async def respond(self, task: str) -> ChatResponse:
        """对外入口：把任务包装成 HumanMessage 后驱动图执行，返回结构化回复。"""
        result = await self._graph.ainvoke({"messages": [HumanMessage(content=task)]})

        content = ""
        for message in reversed(result.get("messages", [])):
            if isinstance(message, AIMessage):
                content = str(message.content)
                break

        return ChatResponse(content=content, tool_calls=result.get("tool_calls", []))
