"""LLM 层统一异常：由 factory、graph、cli 引用。"""


class LLMError(Exception):
    """LLM gateway 层的基类错误。"""


class LLMConfigurationError(LLMError):
    """网关配置缺失（如缺少 API key）时抛出。"""
