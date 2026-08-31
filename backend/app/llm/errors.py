"""Typed failures that LLM provider adapters can safely expose to callers."""

from __future__ import annotations


class LLMGatewayError(Exception):
    """Base class for failures raised by an :class:`LLMGateway` implementation.

    Provider adapters should translate SDK-specific exceptions at their boundary
    instead of relying on their messages or concrete SDK types in the agent
    runtime.
    """


class TransientLLMError(LLMGatewayError):
    """A temporary provider failure that is safe to retry.

    Examples include rate limiting, a transient network failure, or a provider
    5xx response.  The agent graph applies its bounded node retry policy only
    to this explicit category (and to a local timeout).
    """


class NonRetryableLLMError(LLMGatewayError):
    """A provider failure for which retrying the same request will not help.

    Examples include invalid credentials, an invalid provider request, or an
    unsupported model configuration.
    """
