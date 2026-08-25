from __future__ import annotations

import socket
from typing import Any

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from email_agent.email_clients.base import MailClient, MailClientError

if __import__("typing").TYPE_CHECKING:
    from email_agent.models.account import Account

# IMAP 连接默认超时时间（秒），避免网络异常时无限阻塞
DEFAULT_TIMEOUT = 30


class ImapMailClient(MailClient):
    """基于 :mod:`imapclient` 的 IMAP 协议实现。"""

    def __init__(
        self, account: Account, client_cls: Any | None = None
    ) -> None:  # accept MagicMock for tests
        # 调用父类初始化，绑定账号信息
        super().__init__(account)
        # 底层的 IMAPClient 实例，连接前为 None
        self._client: IMAPClient | None = None
        # 可注入的客户端类，默认为 IMAPClient，测试时可传入 MagicMock
        self._client_cls: Any = client_cls or IMAPClient

    def connect(self) -> None:
        """建立 IMAP 连接并登录，幂等（已连接则直接返回）。"""
        # 已连接则跳过，避免重复登录
        if self._client is not None:
            return
        try:
            # 创建底层客户端，传入主机、端口、SSL 和超时配置
            client = self._client_cls(
                self.account.host,
                port=self.account.port,
                ssl=self.account.use_ssl,
                timeout=DEFAULT_TIMEOUT,
            )
            # 使用用户名和授权码登录
            client.login(self.account.username, self.account.password)
            # 选中初始文件夹，后续 fetch 若请求不同文件夹会重新选中
            client.select_folder(self.account.folder)
            self._client = client
        except (IMAPClientError, socket.timeout, TimeoutError, OSError) as exc:  # noqa: UP041
            # 捕获预期的网络/协议异常，包装为 MailClientError 并带上账号名，绝不泄露密码
            raise MailClientError(f"[{self.account.name}] IMAP connect failed: {exc}") from exc
        except MailClientError:
            # 已是包装过的异常，直接透传
            raise
        except Exception as exc:
            # 兜底捕获未预期异常，统一包装，保证上层隔离逻辑一致
            raise MailClientError(f"[{self.account.name}] IMAP connect failed: {exc}") from exc

    def fetch_emails(
        self,
        folder: str,
        since_uid: int,
        limit: int | None = None,
    ) -> list[tuple[int, bytes]]:
        """增量拉取邮件，返回 UID 大于 since_uid 的原始 RFC822 字节。"""
        # 未连接时直接报错，提示调用方先调用 connect()
        if self._client is None:
            raise MailClientError(f"[{self.account.name}] not connected; call connect() first")
        # 参数校验：since_uid 必须为非负整数，limit 若提供必须为正整数
        if not isinstance(since_uid, int) or since_uid < 0:
            msg = f"since_uid must be int >=0, got {since_uid!r}"
            raise ValueError(msg)
        if limit is not None and (not isinstance(limit, int) or limit <= 0):
            msg = f"limit must be positive int or None, got {limit!r}"
            raise ValueError(msg)

        try:
            # 若请求的文件夹与账号默认文件夹不同，需重新选中
            if folder != self.account.folder:
                self._client.select_folder(folder)
            # 构造 IMAP 搜索条件：全量或增量
            # since_uid==0 时全量拉取，否则搜索 UID 大于 since_uid 的邮件
            criteria = ["ALL"] if since_uid == 0 else ["UID", f"{since_uid + 1}:*"]
            uids: list[int] = self._client.search(criteria)  # type: ignore[arg-type]
            # 无新邮件时直接返回空列表
            if not uids:
                return []
            # 按 UID 升序排列，保证处理顺序稳定，limit 截断时取最旧的一批
            uids = sorted(uids)
            if limit is not None:
                uids = uids[:limit]
                if not uids:
                    return []

            # 批量拉取邮件的 RFC822 原始字节
            fetch_data = self._client.fetch(uids, [b"RFC822"])
            result: list[tuple[int, bytes]] = []
            for uid in uids:
                entry = fetch_data.get(uid)
                # 某些 UID 可能已被删除或拉取失败，跳过
                if not entry:
                    continue
                raw = entry.get(b"RFC822")
                if raw is None:
                    continue
                # 仅接受字节类型，imapclient 正常情况下始终返回 bytes
                if isinstance(raw, bytes):
                    result.append((uid, raw))
                # 其他类型忽略，避免污染下游解析
            return result
        except MailClientError:
            # 已包装的异常直接透传
            raise
        except (IMAPClientError, socket.timeout, TimeoutError, OSError) as exc:  # noqa: UP041
            # 网络/协议层异常统一包装
            raise MailClientError(f"[{self.account.name}] IMAP fetch failed: {exc}") from exc
        except Exception as exc:
            # 未预期异常也包装为 MailClientError，保持 service 层的异常隔离逻辑统一
            raise MailClientError(f"[{self.account.name}] IMAP fetch failed: {exc}") from exc

    def close(self) -> None:
        """关闭 IMAP 连接，幂等且尽力而为。"""
        if self._client is None:
            return
        try:
            # 尝试优雅登出
            self._client.logout()
        except Exception:
            # 登出失败不抛异常，避免影响主流程
            pass
        finally:
            # 无论成功与否，都清空引用，标记为已关闭
            self._client = None
