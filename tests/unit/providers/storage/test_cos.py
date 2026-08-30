"""providers.storage.cos 单元测试：对象键构造与配置校验（不联网）。"""

from __future__ import annotations

import pytest

from app.core.settings import CosConfig
from app.providers.storage.cos import CosAttachmentStorage


def test_build_key_is_deterministic_and_sanitized() -> None:
    key = CosAttachmentStorage.build_key(1, 6, 0, "截图 shot.png")

    assert key == "email-attachments/1/6/0-shot.png"
    # 同参数重复构造 → 同键（重传即覆盖，幂等）
    assert CosAttachmentStorage.build_key(1, 6, 0, "截图 shot.png") == key


def test_build_key_empty_filename_falls_back_to_unnamed() -> None:
    assert CosAttachmentStorage.build_key(1, 6, 2, "") == "email-attachments/1/6/2-unnamed"


def test_constructor_requires_full_config() -> None:
    with pytest.raises(ValueError, match="COS storage requires config"):
        CosAttachmentStorage(CosConfig())


def test_constructor_with_full_config_builds_client() -> None:
    config = CosConfig(
        cos_secret_id="AKIDxxx",
        cos_secret_key="secret",
        cos_bucket="bucket-1250000000",
        cos_region="ap-guangzhou",
    )
    storage = CosAttachmentStorage(config)

    assert storage.build_key(1, 1, 0, "a.png") == "email-attachments/1/1/0-a.png"
