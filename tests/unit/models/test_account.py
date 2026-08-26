import pytest

from app.models.account import Account


def test_account_creation_with_required_fields_succeeds():
    acc = Account(
        id=1,
        name="qq",
        host="imap.qq.com",
        username="user@qq.com",
        password="secret",
    )
    assert acc.id == 1
    assert acc.name == "qq"
    assert acc.host == "imap.qq.com"
    assert acc.username == "user@qq.com"
    assert acc.password == "secret"


def test_account_defaults_match_db_schema():
    acc = Account(id=1, name="a", host="h", username="u", password="p")
    assert acc.port == 993
    assert acc.protocol == "imap"
    assert acc.use_ssl is True
    assert acc.folder == "INBOX"
    assert acc.enabled is True
    assert acc.last_sync_uid == 0
    assert acc.last_sync_at is None


def test_account_last_sync_uid_negative_raises():
    with pytest.raises(ValueError):
        Account(id=1, name="n", host="h", username="u", password="p", last_sync_uid=-1)


def test_account_protocol_unsupported_raises():
    with pytest.raises(ValueError):
        Account(id=1, name="n", host="h", username="u", password="p", protocol="pop3")


def test_account_invalid_port_raises():
    with pytest.raises(ValueError):
        Account(id=1, name="n", host="h", username="u", password="p", port=99999)


def test_account_empty_name_raises():
    with pytest.raises(ValueError):
        Account(id=1, name="", host="h", username="u", password="p")
