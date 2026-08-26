"""services 包自包含性回归测试：本包不得依赖 app.db。"""

import os
import subprocess
import sys
from pathlib import Path


def test_import_services_does_not_load_db():
    # 在干净解释器中导入 services 后，db 模块不应被加载（无跨层依赖）
    code = "import app.services, sys; print('app.db' in sys.modules)"
    source_root = Path(__file__).resolve().parents[4] / "backend"
    env = {
        **os.environ,
        "PYTHONPATH": str(source_root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )

    assert result.stdout.strip() == "False"
