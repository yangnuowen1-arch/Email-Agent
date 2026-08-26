"""结构化日志配置：基于 structlog 输出 JSON，并桥接标准库 logging。"""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False


def _build_shared_processors() -> list:
    """返回 structlog 与标准库 logging 共享的处理器链（不含最终渲染器）。

    注意：``filter_by_level`` 不放在这里，因为它需要 stdlib logger 实例，
    而 ``ProcessorFormatter`` 在格式化外来 record 时 ``self.logger`` 为 None；
    标准库 handler 自身已做级别过滤，无需重复。
    """
    return [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]


def configure_logging(log_level: str = "INFO") -> None:
    """配置 structlog（JSON 输出）并桥接标准库 logging，使现有
    ``logging.getLogger(__name__)`` 调用也输出 JSON。

    幂等：重复调用不会叠加 handler。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    shared = _build_shared_processors()

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.setLevel(log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared,
        )
    )
    root.addHandler(handler)

    _CONFIGURED = True
