"""services.email.EmailService.read 单元测试：只负责读，无 DB 操作。"""

from __future__ import annotations

from app.providers.email.base import MailClientError
from app.schemas import AccountSpec, EmailData, RawEmail
from app.services.email import EmailService

_VALID_RAW = [
    (1, b"From: a@b.com\r\nSubject: hi\r\n\r\nbody\r\n"),
    (2, b"From: c@d.com\r\nSubject: ho\r\n\r\nmore\r\n"),
]


class _FakeClient:
    def __init__(self, account, raws):
        self._raws = raws

    def connect(self):
        return None

    def fetch_emails(self, folder, since_uid, limit=None):
        return self._raws

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


async def test_read_returns_email_data_list():
    service = EmailService(client_factory=_factory(_VALID_RAW))
    result = await service.read(_spec(), full=True)

    assert len(result) == 2
    assert all(isinstance(m, EmailData) for m in result)
    assert {m.subject for m in result} == {"hi", "ho"}


async def test_read_skips_messages_that_fail_to_parse():
    # 第二封无法解析时跳过，不中断整批
    def boom(raw_email: RawEmail) -> EmailData:
        if raw_email.uid == 2:
            raise ValueError("boom")
        return EmailData(account_id=raw_email.account_id, uid=raw_email.uid, subject="ok")

    service = EmailService(client_factory=_factory(_VALID_RAW), parser=boom)

    result = await service.read(_spec(), full=True)
    assert len(result) == 1
    assert result[0].subject == "ok"


async def test_read_propagates_mail_client_error():
    class _BoomClient(_FakeClient):
        def connect(self):
            raise MailClientError("[acc] connect failed")

    service = EmailService(client_factory=lambda account: _BoomClient(account, _VALID_RAW))
    try:
        await service.read(_spec(), full=True)
        assert False, "expected MailClientError"
    except MailClientError:
        pass


async def test_read_rejects_bad_limit():
    service = EmailService(client_factory=_factory(_VALID_RAW))
    try:
        await service.read(_spec(), limit=0)
        assert False, "expected ValueError"
    except ValueError:
        pass
