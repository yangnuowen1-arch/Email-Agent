"""腾讯云 COS 附件存储：抓取时上传字节，分析时按 key 拉取。

官方 SDK（cos-python-sdk-v5）为同步客户端，方法经 ``asyncio.to_thread``
桥接为异步；实例由容器显式构造注入，无模块级全局单例。
桶建议私有读写，统一经 SDK 凭证访问，URL 仅作标识与展示。
"""

from __future__ import annotations

import asyncio
import re

from qcloud_cos import CosConfig as SdkConfig
from qcloud_cos import CosS3Client

from app.core.settings import CosConfig

# 对象键中的文件名仅保留安全字符，其余归一为下划线
_FILENAME_UNSAFE = re.compile(r"[^0-9A-Za-z._-]+")


class CosAttachmentStorage:
    """COS 附件存储门面：upload 返回访问 URL，fetch 按 key 取回字节。"""

    def __init__(self, config: CosConfig) -> None:
        missing = [
            key
            for key in ("cos_secret_id", "cos_secret_key", "cos_bucket", "cos_region")
            if not getattr(config, key)
        ]
        if missing:
            msg = f"COS storage requires config: {missing}"
            raise ValueError(msg)

        self._bucket = config.cos_bucket
        self._region = config.cos_region
        self._client = CosS3Client(
            SdkConfig(
                Region=config.cos_region,
                SecretId=config.cos_secret_id,
                SecretKey=config.cos_secret_key,
                Scheme="https",
            )
        )

    @staticmethod
    def build_key(account_id: int, uid: int, index: int, filename: str) -> str:
        """构造确定性对象键：同一邮件重传即覆盖同名对象（幂等）。"""
        safe = _FILENAME_UNSAFE.sub("_", filename).strip("._") or "unnamed"
        return f"email-attachments/{account_id}/{uid}/{index}-{safe}"

    def _url(self, key: str) -> str:
        return f"https://{self._bucket}.cos.{self._region}.myqcloud.com/{key}"

    async def upload(self, key: str, content: bytes, content_type: str) -> str:
        """上传附件字节，返回对象访问 URL。"""
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
        )
        return self._url(key)

    async def fetch(self, key: str) -> bytes:
        """按对象键拉取附件字节（分析时还原内容用）。"""
        resp = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=key)
        return resp["Body"].get_raw_stream().read()
