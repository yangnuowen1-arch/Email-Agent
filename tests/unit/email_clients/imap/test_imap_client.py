from unittest.mock import MagicMock, patch

import pytest

from email_agent.email_clients.base import MailClientError
from email_agent.email_clients.imap.client import DEFAULT_TIMEOUT, ImapMailClient
from email_agent.models.account import Account

try:
    from imapclient.exceptions import IMAPClientError
except ImportError:  # pragma: no cover - for type only
    IMAPClientError = Exception  # type: ignore[assignment,misc]


def _account(**overrides) -> Account:
    base = dict(
        id=1,
        name="test-account",
        host="imap.example.com",
        username="u@example.com",
        password="s3cr3t",
        folder="INBOX",
    )
    base.update(overrides)
    return Account(**base)  # type: ignore[arg-type]


def _mock_imap_client():
    mock = MagicMock()
    mock.search.return_value = []
    mock.fetch.return_value = {}
    return mock


def test_connect_creates_imapclient_with_correct_params():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()

    mock_cls.assert_called_once_with(
        acc.host, port=acc.port, ssl=acc.use_ssl, timeout=DEFAULT_TIMEOUT
    )
    mock_instance.login.assert_called_once_with(acc.username, acc.password)
    mock_instance.select_folder.assert_called_once_with(acc.folder)


def test_connect_uses_timeout_30():
    assert DEFAULT_TIMEOUT == 30


def test_fetch_empty_mailbox_returns_empty():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.search.return_value = []
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    result = client.fetch_emails("INBOX", since_uid=0)

    assert result == []
    mock_instance.search.assert_called_once()
    mock_instance.fetch.assert_not_called()


def test_fetch_since_uid_zero_uses_all():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.search.return_value = [1, 2]
    mock_instance.fetch.return_value = {1: {b"RFC822": b"raw1"}, 2: {b"RFC822": b"raw2"}}
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    result = client.fetch_emails(acc.folder, since_uid=0)

    assert mock_instance.search.call_args[0][0] == ["ALL"]
    assert result == [(1, b"raw1"), (2, b"raw2")]


def test_fetch_since_uid_filters_via_search_arg():
    acc = _account(last_sync_uid=10)
    mock_instance = _mock_imap_client()
    mock_instance.search.return_value = [11, 12]
    mock_instance.fetch.return_value = {11: {b"RFC822": b"a"}, 12: {b"RFC822": b"b"}}
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    # clear select_folder call from connect
    mock_instance.select_folder.reset_mock()
    result = client.fetch_emails("INBOX", since_uid=10)

    mock_instance.search.assert_called_once_with(["UID", "11:*"])
    assert result == [(11, b"a"), (12, b"b")]
    # folder same as account.folder, no extra select_folder
    mock_instance.select_folder.assert_not_called()


def test_fetch_different_folder_triggers_select():
    acc = _account(folder="INBOX")
    mock_instance = _mock_imap_client()
    mock_instance.search.return_value = [1]
    mock_instance.fetch.return_value = {1: {b"RFC822": b"x"}}
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    mock_instance.select_folder.reset_mock()
    client.fetch_emails("Archive", since_uid=0)

    mock_instance.select_folder.assert_called_once_with("Archive")


def test_fetch_limit_truncates_sorted_uids():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.search.return_value = [5, 1, 3, 2, 4]
    mock_instance.fetch.return_value = {1: {b"RFC822": b"1"}, 2: {b"RFC822": b"2"}}
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    result = client.fetch_emails("INBOX", since_uid=0, limit=2)

    # search unordered, fetch should be called with sorted first 2
    mock_instance.fetch.assert_called_once()
    called_uids = mock_instance.fetch.call_args[0][0]
    assert called_uids == [1, 2]
    assert result == [(1, b"1"), (2, b"2")]
    # ensure sorted
    assert [uid for uid, _ in result] == sorted([uid for uid, _ in result])


def test_fetch_maps_rfc822_bytes_correctly():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.search.return_value = [10, 20]
    mock_instance.fetch.return_value = {
        10: {b"RFC822": b"raw10", b"SEQ": 1},
        20: {b"RFC822": b"raw20"},
    }
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    result = client.fetch_emails("INBOX", since_uid=0)

    assert result == [(10, b"raw10"), (20, b"raw20")]
    mock_instance.fetch.assert_called_once_with([10, 20], [b"RFC822"])


def test_fetch_skips_missing_rfc822():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.search.return_value = [1, 2, 3]
    mock_instance.fetch.return_value = {
        1: {b"RFC822": b"ok"},
        2: {b"SEQ": 2},  # missing RFC822
        3: {b"RFC822": b"ok3"},
    }
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    result = client.fetch_emails("INBOX", since_uid=0)

    assert result == [(1, b"ok"), (3, b"ok3")]


def test_fetch_empty_after_limit_returns_empty():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.search.return_value = [1, 2]
    mock_instance.fetch.return_value = {1: {b"RFC822": b"a"}, 2: {b"RFC822": b"b"}}
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    result = client.fetch_emails("INBOX", since_uid=0, limit=5)
    assert len(result) == 2
    mock_instance.fetch.assert_called_once()


def test_fetch_invalid_limit_raises():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_cls = MagicMock(return_value=mock_instance)
    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    with pytest.raises(ValueError, match="limit"):
        client.fetch_emails("INBOX", since_uid=0, limit=0)
    with pytest.raises(ValueError, match="limit"):
        client.fetch_emails("INBOX", since_uid=0, limit=-1)


def test_fetch_not_connected_raises():
    acc = _account()
    mock_cls = MagicMock()
    client = ImapMailClient(acc, client_cls=mock_cls)
    with pytest.raises(MailClientError, match="not connected"):
        client.fetch_emails("INBOX", since_uid=0)


def test_connect_login_failure_wraps_mail_client_error():
    acc = _account(password="s3cr3t")
    mock_instance = MagicMock()
    mock_instance.login.side_effect = IMAPClientError("auth failed")
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    with pytest.raises(MailClientError) as exc_info:
        client.connect()

    msg = str(exc_info.value)
    assert acc.name in msg
    assert acc.password not in msg
    assert "connect failed" in msg.lower()


def test_connect_socket_timeout_wraps():
    acc = _account()
    mock_instance = MagicMock()
    mock_instance.login.side_effect = TimeoutError("timed out")
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    with pytest.raises(MailClientError) as exc_info:
        client.connect()

    assert acc.name in str(exc_info.value)
    assert acc.password not in str(exc_info.value)


def test_search_timeout_wraps_mail_client_error():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.search.side_effect = TimeoutError("timed out")
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    with pytest.raises(MailClientError) as exc_info:
        client.fetch_emails("INBOX", since_uid=0)

    assert acc.name in str(exc_info.value)
    assert acc.password not in str(exc_info.value)


def test_fetch_imap_error_wraps():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.search.side_effect = IMAPClientError("search failed")
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    with pytest.raises(MailClientError) as exc_info:
        client.fetch_emails("INBOX", since_uid=0)

    assert "fetch failed" in str(exc_info.value).lower()


def test_close_calls_logout_and_idempotent():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    client.close()
    mock_instance.logout.assert_called_once()
    # second close should not call logout again
    mock_instance.logout.reset_mock()
    client.close()
    mock_instance.logout.assert_not_called()


def test_close_logout_exception_suppressed():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.logout.side_effect = Exception("logout boom")
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    # should not raise
    client.close()
    assert client._client is None


def test_context_manager_calls_connect_and_close():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_instance.search.return_value = []
    mock_cls = MagicMock(return_value=mock_instance)

    with ImapMailClient(acc, client_cls=mock_cls) as c:
        assert isinstance(c, ImapMailClient)
        # connect called
        mock_cls.assert_called_once()
        # fetch inside context
        result = c.fetch_emails("INBOX", since_uid=0)
        assert result == []

    mock_instance.logout.assert_called_once()


def test_connect_idempotent_second_call_noop():
    acc = _account()
    mock_instance = _mock_imap_client()
    mock_cls = MagicMock(return_value=mock_instance)

    client = ImapMailClient(acc, client_cls=mock_cls)
    client.connect()
    mock_cls.reset_mock()
    client.connect()
    mock_cls.assert_not_called()


def test_patch_style_works_with_unittest_mock():
    acc = _account()
    with patch("email_agent.email_clients.imap.client.IMAPClient") as MockIMAP:
        mock_inst = MagicMock()
        mock_inst.search.return_value = [1]
        mock_inst.fetch.return_value = {1: {b"RFC822": b"data"}}
        MockIMAP.return_value = mock_inst

        # Use default client_cls (IMAPClient patched)
        client = ImapMailClient(acc)
        client.connect()
        result = client.fetch_emails("INBOX", since_uid=0)
        assert result == [(1, b"data")]
        MockIMAP.assert_called_once()
