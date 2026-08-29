"""services.email.EmailService.receive 单元测试：只负责接收与解析，无 DB 操作。"""

from __future__ import annotations

import threading

import pytest

from app.providers.email.base import MailClientError
from app.schemas import AccountSpec, EmailData, RawEmail
from app.services.email import EmailService

_VALID_RAW = [
    (1, b"From: a@b.com\r\nSubject: hi\r\n\r\nbody\r\n"),
    (2, b"From: c@d.com\r\nSubject: ho\r\n\r\nmore\r\n"),
]


class _FakeClient:
    """模拟 provider：连接后把预设的原始批次回调给 service，随后返回。"""

    def __init__(self, account, raws):
        self._raws = raws

    def connect(self):
        return None

    def receive_emails(self, folder, on_batch, stop_event=None):
        # 回调异常直接向上传播，与真实 provider 的"回调失败即批次不确认"一致
        if self._raws:
            on_batch(list(self._raws))

    def close(self):
        return None


def _factory(raws):
    def factory(account):
        return _FakeClient(account, raws)

    return factory


def _spec() -> AccountSpec:
    return AccountSpec(
        account_id=1,
        name="acc",
        last_sync_uid=0,
        host="imap.example.com",
        username="u",
        password="p",
    )


async def test_receive_delivers_parsed_batches_to_callback():
    received: list[list[EmailData]] = []

    async def on_batch(messages):
        received.append(messages)

    service = EmailService(client_factory=_factory(_VALID_RAW))
    await service.receive(_spec(), on_batch, threading.Event())

    assert len(received) == 1
    assert all(isinstance(m, EmailData) for m in received[0])
    assert [m.subject for m in received[0]] == ["hi", "ho"]


async def test_receive_skips_messages_that_fail_to_parse():
    # 第二封无法解析时跳过，不中断整批
    def boom(raw_email: RawEmail) -> EmailData:
        if raw_email.uid == 2:
            raise ValueError("boom")
        return EmailData(account_id=raw_email.account_id, uid=raw_email.uid, subject="ok")

    received: list[list[EmailData]] = []

    async def on_batch(messages):
        received.append(messages)

    service = EmailService(client_factory=_factory(_VALID_RAW), parser=boom)
    await service.receive(_spec(), on_batch, threading.Event())

    assert [m.subject for m in received[0]] == ["ok"]


async def test_receive_confirms_batch_even_when_all_parse_fail():
    # 全部解析失败时仍以空批次回调：provider 据此确认该批，避免无限重推
    def boom(raw_email: RawEmail) -> EmailData:
        raise ValueError("boom")

    received: list[list[EmailData]] = []

    async def on_batch(messages):
        received.append(messages)

    service = EmailService(client_factory=_factory(_VALID_RAW), parser=boom)
    await service.receive(_spec(), on_batch, threading.Event())

    assert received == [[]]


async def test_receive_propagates_store_failure_as_backpressure():
    # 落库回调抛异常：异常经线程桥接传回 provider 侧，receive 向上抛
    async def on_batch(messages):
        raise RuntimeError("store down")

    service = EmailService(client_factory=_factory(_VALID_RAW))
    with pytest.raises(RuntimeError, match="store down"):
        await service.receive(_spec(), on_batch, threading.Event())


async def test_receive_propagates_mail_client_error():
    class _BoomClient(_FakeClient):
        def connect(self):
            raise MailClientError("[acc] connect failed")

    async def on_batch(messages):
        return None

    service = EmailService(client_factory=lambda account: _BoomClient(account, _VALID_RAW))
    with pytest.raises(MailClientError):
        await service.receive(_spec(), on_batch, threading.Event())
