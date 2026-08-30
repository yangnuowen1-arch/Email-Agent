"""LLM 层：工厂与异常，供 container / graph / rag / cli 引用。"""

from .errors import LLMConfigurationError, LLMError
from .factory import build_chat_model, build_embedding_model

__all__ = [
    "build_chat_model",
    "build_embedding_model",
    "LLMConfigurationError",
    "LLMError",
]
