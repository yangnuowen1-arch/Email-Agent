"""对象存储提供者：附件字节的外部存储入口，按后端具体实现路由。"""

from app.providers.storage.cos import CosAttachmentStorage

__all__ = ["CosAttachmentStorage"]
