"""Smoke tests: package importability and CLI entry availability."""

from __future__ import annotations

import os
import subprocess
import sys

from email_agent import __version__


def test_package_version_is_exposed() -> None:
    assert __version__ == "0.1.0"


def test_cli_help_exits_zero() -> None:
    # 显式注入 PYTHONPATH=src，保证未安装包时子进程同样可导入
    env = {
        **os.environ,
        "PYTHONPATH": "src" + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }

    result = subprocess.run(
        [sys.executable, "-m", "email_agent", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0

    assert "--limit" in result.stdout

    assert "--full" in result.stdout
