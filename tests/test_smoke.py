"""Smoke tests: package importability and CLI entry availability."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app import __version__


def test_package_version_is_exposed() -> None:
    assert __version__ == "0.1.0"


def test_cli_help_exits_zero() -> None:
    # 使用绝对源码路径，保证从仓库内任意工作目录执行时都能导入。
    source_root = Path(__file__).resolve().parents[1] / "backend"
    env = {
        **os.environ,
        "PYTHONPATH": str(source_root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }

    result = subprocess.run(
        [sys.executable, "-m", "app", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0

    # 新设计：统一 agent 入口 + listen 常驻监听，均以子命令暴露
    assert "agent" in result.stdout
    assert "listen" in result.stdout
