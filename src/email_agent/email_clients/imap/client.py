from __future__ import annotations

import contextlib
from typing import Any

from imapclient import IMAPClient

from email_agent.email_clients.base import AccountConfig, MailClient, MailClientError

# IMAP 连接默认超时时间（秒），避免网络异常时无限阻塞
DEFAULT_TIMEOUT = 30


class ImapMailClient(MailClient):
    """基于 :mod:`imapclient` 的 IMAP 协议实现。"""

    def __init__(self, account: AccountConfig, client_cls: Any | None = None) -> None:
        super().__init__(account)

        # 底层的 IMAPClient 实例，连接前为 None
        self._client: IMAPClient | None = None

        # 可注入的客户端类，默认为真实实现；测试传 MagicMock 以隔离网络
        self._client_cls: Any = client_cls or IMAPClient

    def connect(self) -> None:
        """建立 IMAP 连接并登录，幂等（已连接则直接返回）。"""
        if self._client is not None:
            return

        try:
            client = self._client_cls(
                self.account.host,
                port=self.account.port,
                ssl=self.account.use_ssl,
                timeout=DEFAULT_TIMEOUT,
            )

            client.login(self.account.username, self.account.password)

            # 选中默认文件夹；fetch 请求其他文件夹时会重新选中
            client.select_folder(self.account.folder)
            self._client = client

        except Exception as exc:
            # 统一包装并携带账号名，绝不泄露密码；上层据此做失败隔离
            raise MailClientError(f"[{self.account.name}] IMAP connect failed: {exc}") from exc

    def fetch_emails(
        self,
        folder: str,
        since_uid: int,
        limit: int | None = None,
    ) -> list[tuple[int, bytes]]:
        """增量拉取邮件，返回 UID 大于 since_uid 的原始 RFC822 字节。"""
        if self._client is None:
            raise MailClientError(f"[{self.account.name}] not connected; call connect() first")

        if not isinstance(since_uid, int) or since_uid < 0:
            msg = f"since_uid must be int >=0, got {since_uid!r}"
            raise ValueError(msg)

        if limit is not None and (not isinstance(limit, int) or limit <= 0):
            msg = f"limit must be positive int or None, got {limit!r}"
            raise ValueError(msg)

        try:
            if folder != self.account.folder:
                self._client.select_folder(folder)

            # since_uid==0 全量拉取，否则只搜索断点之后的 UID
            criteria = ["ALL"] if since_uid == 0 else ["UID", f"{since_uid + 1}:*"]
            uids: list[int] = self._client.search(criteria)  # type: ignore[arg-type]
            if not uids:
                return []

            # 升序保证处理顺序稳定；limit 截断时取最旧的一批
            uids = sorted(uids)
            if limit is not None:
                uids = uids[:limit]

            fetch_data = self._client.fetch(uids, [b"RFC822"])

            result: list[tuple[int, bytes]] = []
            for uid in uids:
                entry = fetch_data.get(uid)

                # 已删除/缺失/非 bytes 的条目跳过，不污染下游解析
                raw = entry.get(b"RFC822") if entry else None
                if isinstance(raw, bytes):
                    result.append((uid, raw))

            return result

        except Exception as exc:
            # 统一包装为 MailClientError，保持 service 层异常隔离逻辑一致
            raise MailClientError(f"[{self.account.name}] IMAP fetch failed: {exc}") from exc

    def close(self) -> None:
        """关闭 IMAP 连接，幂等且尽力而为。"""
        if self._client is None:
            return

        # 登出失败不影响主流程，但必须清空引用以标记已关闭
        with contextlib.suppress(Exception):
            self._client.logout()

        self._client = None
