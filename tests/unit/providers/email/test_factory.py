import pytest

from app.models.account import Account
from app.providers.email.base import MailClient
from app.providers.email.factory import create_client, register_client
from app.providers.email.imap.client import ImapMailClient


def _account(**overrides) -> Account:
    base = dict(
        id=1, name="test", host="imap.example.com", username="u@example.com", password="secret"
    )
    base.update(overrides)
    return Account(**base)  # type: ignore[arg-type]


def test_factory_create_imap_returns_imap_client():
    acc = _account(protocol="imap")
    client = create_client(acc)
    assert isinstance(client, ImapMailClient)
    assert isinstance(client, MailClient)


def test_factory_case_insensitive():
    # bypass model validation to test upper case;
    # Account validates protocol, so we test factory normalization
    # by manually calling with an object that has .protocol attribute
    class FakeAcc:
        protocol = "IMAP"
        id = 1
        name = "fake"
        host = "h"
        port = 993
        username = "u"
        password = "p"
        use_ssl = True
        folder = "INBOX"

    fake = FakeAcc()  # type: ignore[assignment]
    client = create_client(fake)  # type: ignore[arg-type]
    assert isinstance(client, ImapMailClient)


def test_factory_unsupported_raises_value_error():
    class FakeAcc:
        protocol = "pop3"
        id = 1
        name = "fake"
        host = "h"
        port = 993
        username = "u"
        password = "p"
        use_ssl = True
        folder = "INBOX"

    fake = FakeAcc()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="unsupported protocol"):
        create_client(fake)  # type: ignore[arg-type]


def test_factory_error_message_includes_protocol():
    class FakeAcc:
        protocol = "exchange"
        id = 1
        name = "x"
        host = "h"
        port = 993
        username = "u"
        password = "p"
        use_ssl = True
        folder = "INBOX"

    fake = FakeAcc()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="exchange"):
        create_client(fake)  # type: ignore[arg-type]


def test_register_client_allows_new_protocol():
    class DummyClient(MailClient):
        def connect(self) -> None:
            pass

        def fetch_emails(self, folder, since_uid, limit=None):
            return []

        def close(self) -> None:
            pass

    register_client("dummy", DummyClient)

    class FakeAcc:
        protocol = "dummy"
        id = 1
        name = "x"
        host = "h"
        port = 993
        username = "u"
        password = "p"
        use_ssl = True
        folder = "INBOX"

    fake = FakeAcc()  # type: ignore[assignment]
    client = create_client(fake)  # type: ignore[arg-type]
    assert isinstance(client, DummyClient)
    # also case insensitive registration
    fake2 = type(  # type: ignore[misc]
        "A",
        (),
        {
            "protocol": "DUMMY",
            "id": 1,
            "name": "x",
            "host": "h",
            "port": 993,
            "username": "u",
            "password": "p",
            "use_ssl": True,
            "folder": "INBOX",
        },
    )()
    client2 = create_client(fake2)  # type: ignore[arg-type]
    assert isinstance(client2, DummyClient)

    # cleanup: remove dummy to not pollute other tests
    from app.providers.email.factory import _REGISTRY

    _REGISTRY.pop("dummy", None)


def test_register_client_rejects_non_mailclient():
    with pytest.raises(TypeError):
        register_client("bad", object)  # type: ignore[arg-type]
