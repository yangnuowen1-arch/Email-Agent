"""分析图异常层级。

LangGraph 不做异常包装：节点抛出的异常原样从 ``ainvoke`` 传播。
本模块只定义"可预期的"图内错误；逻辑代码错误不在此捕获包装，
保持原样冒泡，从而与 LLM 错误天然区分：

- ``except AnalysisGraphError`` → 图内可预期失败（当前仅 LLM 调用类）
- 其他异常 → 代码 / 框架 bug，不应被调用方静默吞掉
"""


class AnalysisGraphError(Exception):
    """分析图内可预期错误的基类，调用方的统一捕获点。"""


class LLMInvocationError(AnalysisGraphError):
    """LLM 调用类错误：超时、网络/限流、结构化输出解析失败、空返回。

    构造时以 ``raise ... from exc`` 传递，原始异常保留在 ``__cause__``。
    """
