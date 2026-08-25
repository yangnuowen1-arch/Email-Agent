from unittest.mock import MagicMock

import pytest

from email_agent.email_clients.base import MailClient, MailClientError
from email_agent.models.account import Account


def _account(**overrides) -> Account:
    base = dict(
        id=1, name="test", host="imap.example.com", username="u@example.com", password="secret"
    )
    base.update(overrides)
    return Account(**base)  # type: ignore[arg-type]


def test_mail_client_is_abstract_cannot_instantiate():
    with pytest.raises(TypeError):
        MailClient(_account())  # type: ignore[abstract]


def test_mail_client_error_is_exception():
    err = MailClientError("boom")
    assert isinstance(err, Exception)


def test_mail_client_stores_account():
    acc = _account()

    class FakeClient(MailClient):
        def connect(self) -> None:
            pass

        def fetch_emails(self, folder: str, since_uid: int, limit: int | None = None):
            return []

        def close(self) -> None:
            pass

    c = FakeClient(acc)
    assert c.account is acc


def test_mail_client_context_manager_calls_connect_and_close():
    acc = _account()

    class FakeClient(MailClient):
        def __init__(self, account):
            super().__init__(account)
            self.connect_called = False
            self.close_called = False

        def connect(self) -> None:
            self.connect_called = True

        def fetch_emails(self, folder, since_uid, limit=None):
            return []

        def close(self) -> None:
            self.close_called = True

    client = FakeClient(acc)
    with client as c:
        assert c is client
        assert client.connect_called is True
    assert client.close_called is True


def test_mail_client_context_manager_close_even_on_exception():
    acc = _account()

    class FakeClient(MailClient):
        def connect(self) -> None:
            pass

        def fetch_emails(self, folder, since_uid, limit=None):
            return []

        def close(self) -> None:
            self.close_mock()

        def __init__(self, account):
            super().__init__(account)
            self.close_mock = MagicMock()

    client = FakeClient(acc)
    with pytest.raises(RuntimeError), client:
        raise RuntimeError("inside")

    client.close_mock.assert_called_once()


def test_mail_client_exit_does_not_suppress_exception():
    acc = _account()

    class FakeClient(MailClient):
        def connect(self) -> None:
            pass

        def fetch_emails(self, folder, since_uid, limit=None):
            return []

        def close(self) -> None:
            pass

    client = FakeClient(acc)
    result = client.__exit__(None, None, None)
    assert result is False
