"""Agent 演示入口：本地、无网络的示例流程。

独立于邮件同步 CLI（python -m app），
通过 python -m app.agent 运行。
"""

from __future__ import annotations

import asyncio

from app.agent import EmailAgent
from app.core import get_settings


def main() -> None:
    """运行本地演示流程，验证 agent 骨架可用。"""
    agent = EmailAgent(get_settings())

    response = asyncio.run(agent.respond("Draft a concise welcome email."))

    print(response.text)
