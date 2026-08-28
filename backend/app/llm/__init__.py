"""LLM 层：工厂与异常，供 container / graph / cli 引用。"""

from .errors import LLMConfigurationError, LLMError
from .factory import build_chat_model

__all__ = [
    "build_chat_model",
    "LLMConfigurationError",
    "LLMError",
]
