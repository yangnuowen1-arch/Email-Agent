from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from app.providers.email.base import (
    AccountConfig,
    BatchCallback,
    MailClient,
    MailClientAuthError,
    MailClientError,
)

logger = logging.getLogger(__name__)

# IMAP 连接默认超时时间（秒），避免网络异常时无限阻塞
DEFAULT_TIMEOUT = 30

# IDLE 重发/健康检查默认周期（秒）：至多 29 分钟会被服务器踢连接，
# 60 秒既有较快的丢事件兜底，也让停止监听的等待上限可控
DEFAULT_IDLE_PING_INTERVAL = 60.0


class ImapMailClient(MailClient):
    """基于 :mod:`imapclient` 的 IMAP 协议实现。

    通过 IMAP IDLE（RFC 2177）实现阻塞式接收：连接后进入 IDLE 挂起等待服务器
    推送，每 ``idle_ping_interval`` 秒主动发送 DONE 重发 IDLE——该周期既是连接
    健康检查（ping），醒来后的轻量 UID 搜索也是丢事件的兜底。服务器不支持 IDLE
    时自动退化为按同周期轮询。
    """

    def __init__(
        self,
        account: AccountConfig,
        client_cls: Any | None = None,
        idle_ping_interval: float = DEFAULT_IDLE_PING_INTERVAL,
        backoff_initial_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
    ) -> None:
        super().__init__(account)

        if idle_ping_interval <= 0:
            msg = f"idle_ping_interval must be positive, got {idle_ping_interval!r}"
            raise ValueError(msg)
        if backoff_initial_seconds <= 0 or backoff_max_seconds < backoff_initial_seconds:
            msg = (
                "backoff seconds invalid: "
                f"initial={backoff_initial_seconds!r} max={backoff_max_seconds!r}"
            )
            raise ValueError(msg)

        # 底层的 IMAPClient 实例，连接前为 None
        self._client: IMAPClient | None = None

        # 可注入的客户端类，默认为真实实现；测试传 MagicMock 以隔离网络
        self._client_cls: Any = client_cls or IMAPClient

        # 当前已选中的文件夹（连接级状态），未选中时为 None
        self._selected_folder: str | None = None

        # 本会话已确认推送的最大 uid；None 表示基线尚未确定。
        # 跨重连保留，保证断线期间不丢不重；receive_emails 入口重置
        self._last_uid: int | None = None

        # 接收循环调优参数（秒）：IDLE 重发周期、重连退避起点与上限
        self._idle_ping_interval = idle_ping_interval
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds

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

            # 选中默认文件夹；接收其他文件夹时会重新选中
            client.select_folder(self.account.folder)
            self._client = client
            self._selected_folder = self.account.folder

        except LoginError as exc:
            # 认证失败不可恢复：包装后向上抛出，重连循环不得吞掉
            self._client = None
            self._selected_folder = None
            raise MailClientAuthError(
                f"[{self.account.name}] IMAP login failed: {exc}"
            ) from exc

        except Exception as exc:
            # 统一包装并携带账号名，绝不泄露密码；上层据此做失败隔离
            self._client = None
            self._selected_folder = None
            raise MailClientError(f"[{self.account.name}] IMAP connect failed: {exc}") from exc

    def receive_emails(
        self,
        folder: str,
        on_batch: BatchCallback,
        stop_event: threading.Event | None = None,
    ) -> None:
        """阻塞式接收新邮件（IMAP IDLE），直到 stop_event 置位或登录失败。

        - 基线在首次连接后确定：只推送本会话开始之后新到的邮件
        - 回调成功返回才推进内部断点；回调异常时退避后重推同一批
        - 连接级故障自动重连（指数退避），重连后从已推进断点继续
        """
        if not callable(on_batch):
            msg = f"on_batch must be callable, got {on_batch!r}"
            raise ValueError(msg)

        # 新会话重新确定基线；会话内的重连保留 _last_uid 以保证不丢不重
        self._last_uid = None
        backoff = self._backoff_initial

        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    self._ensure_connected(folder)
                    self._push_new(folder, on_batch)
                    self._idle_or_sleep(stop_event)

                except MailClientAuthError:
                    # 登录失败等不可恢复错误：重连无意义，向上抛出
                    raise

                except MailClientError as exc:
                    # 连接级故障：关闭连接，指数退避后重连；已确认进度保留
                    logger.warning(
                        "imap_receive_recoverable_error account=%s error=%s",
                        self.account.name,
                        exc,
                    )
                    self.close()
                    self._backoff_sleep(stop_event, backoff)
                    backoff = min(backoff * 2, self._backoff_max)

                except Exception:
                    # 回调失败（如落库异常）：连接仍然健康，退避后重推同一批
                    logger.exception(
                        "imap_receive_callback_failed account=%s", self.account.name
                    )
                    self._backoff_sleep(stop_event, backoff)
                    backoff = min(backoff * 2, self._backoff_max)

                else:
                    # 完整健康的一轮（搜索+等待无异常），重置退避
                    backoff = self._backoff_initial

        finally:
            # 无论正常退出、停止还是不可恢复异常，都释放连接（幂等）
            self.close()

    def close(self) -> None:
        """关闭 IMAP 连接，幂等且尽力而为；已确认的接收进度不受影响。"""
        if self._client is None:
            return

        # 登出失败不影响主流程，但必须清空引用以标记已关闭
        with contextlib.suppress(Exception):
            self._client.logout()

        self._client = None
        self._selected_folder = None

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _ensure_connected(self, folder: str) -> None:
        """确保连接可用且目标文件夹已选中；断线时重连。"""
        if self._client is None:
            self.connect()

        if self._selected_folder != folder:
            self._client.select_folder(folder)
            self._selected_folder = folder

    def _push_new(self, folder: str, on_batch: BatchCallback) -> None:
        """搜索并推送 uid 大于内部断点的新邮件；成功回调后才推进断点。

        首次调用时以文件夹内当前最大 uid 为基线（只收之后新到的邮件）。
        """
        client = self._client

        try:
            if self._last_uid is None:
                # 基线：连接时刻文件夹内已有的最大 uid，存量不推送
                existing: list[int] = client.search(["ALL"])
                self._last_uid = max(existing, default=0)
                return

            # 防御 IMAP ``x:*`` 在无新邮件时命中最后一条的 quirk：客户端再过滤一次
            criteria = ["UID", f"{self._last_uid + 1}:*"]
            uids = [u for u in client.search(criteria) if u > self._last_uid]
            if not uids:
                return

            # 升序保证处理顺序稳定
            uids.sort()
            fetch_data = client.fetch(uids, [b"RFC822"])

            batch: list[tuple[int, bytes]] = []
            for uid in uids:
                entry = fetch_data.get(uid)

                # 已删除/缺失/非 bytes 的条目跳过，不污染下游解析
                raw = entry.get(b"RFC822") if entry else None
                if isinstance(raw, bytes):
                    batch.append((uid, raw))

            if not batch:
                return

            # 回调成功返回才确认本批；抛异常则不推进断点，下轮重推
            on_batch(batch)
            self._last_uid = batch[-1][0]

        except MailClientError:
            raise

        except Exception as exc:
            # 搜索/取件失败统一按连接级故障处理，交由外层重连
            raise MailClientError(f"[{self.account.name}] IMAP fetch failed: {exc}") from exc

    def _idle_or_sleep(self, stop_event: threading.Event | None) -> None:
        """等待新邮件：有 IDLE 能力则挂起一个 ping 周期，否则退化为轮询间隔。"""
        client = self._client

        if not client.has_capability("IDLE"):
            # 不支持 IDLE：按 ping 周期轮询（外层每轮都会搜索新邮件）
            self._backoff_sleep(stop_event, self._idle_ping_interval)
            return

        try:
            client.idle()
            try:
                # 至多挂起一个 ping 周期：收到 EXISTS 提前醒来，
                # 超时醒来则充当健康检查 ping（随后外层会重新搜索）
                client.idle_check(timeout=self._idle_ping_interval)
            finally:
                # DONE 发送失败视为连接故障，交由外层重连
                with contextlib.suppress(Exception):
                    client.idle_done()

        except Exception as exc:
            raise MailClientError(f"[{self.account.name}] IMAP idle failed: {exc}") from exc

    @staticmethod
    def _backoff_sleep(stop_event: threading.Event | None, seconds: float) -> None:
        """可中断的退避等待：stop_event 置位时立即返回。"""
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            threading.Event().wait(seconds)
