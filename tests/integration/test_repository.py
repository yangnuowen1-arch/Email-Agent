import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.message import EmailMessage
from app.repository.email_accounts import AccountStore
from app.repository.emails import EmailStore

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://admin:123456@192.168.31.25:5432/email_agent"
)


@pytest.fixture(scope="module")
def engine():
    try:
        eng = create_engine(DATABASE_URL, future=True)
        with eng.connect() as c:
            c.execute(__import__("sqlalchemy").text("select 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PG not available at {DATABASE_URL}: {exc}")
    yield eng
    eng.dispose()


def _create_account(engine, *, enabled=True, last_sync_uid=0):
    name = f"test_acc_{uuid.uuid4().hex[:8]}"
    acc = Account(
        name=name,
        host="imap.example.com",
        username=f"{name}@example.com",
        password="secret",
        enabled=enabled,
        last_sync_uid=last_sync_uid,
    )
    with Session(engine) as s:
        s.add(acc)
        s.flush()
        acc_id = acc.id
        s.commit()
    return acc_id, name


def _cleanup(engine, acc_ids):
    if not acc_ids:
        return
    with Session(engine) as s:
        s.query(EmailMessage).filter(EmailMessage.account_id.in_(acc_ids)).delete(
            synchronize_session=False
        )
        s.query(Account).filter(Account.id.in_(acc_ids)).delete(synchronize_session=False)
        s.commit()


def test_get_enabled_accounts_filters_disabled(engine):
    acc_ids = []
    try:
        id1, _ = _create_account(engine, enabled=True)
        id2, _ = _create_account(engine, enabled=False)
        id3, _ = _create_account(engine, enabled=True)
        acc_ids.extend([id1, id2, id3])

        with Session(engine) as s:
            accounts = AccountStore(s).get_enabled_accounts()
        ids = {a.id for a in accounts}
        assert id1 in ids
        assert id3 in ids
        assert id2 not in ids
        # disabled account should not appear at all
        for a in accounts:
            assert a.enabled is True
    finally:
        _cleanup(engine, acc_ids)


def test_bulk_insert_and_idempotent(engine):
    acc_ids = []
    try:
        acc_id, _ = _create_account(engine)
        acc_ids.append(acc_id)

        now = datetime.now(UTC)
        msgs = [
            EmailMessage(
                account_id=acc_id,
                uid=1001,
                message_id="<msg1@example.com>",
                subject="hello",
                sender="from@example.com",
                recipients=["to@example.com"],
                sent_at=now,
                text_body="plain 1",
                html_body="<p>1</p>",
                fetched_at=now,
            ),
            EmailMessage(
                account_id=acc_id,
                uid=1002,
                message_id="<msg2@example.com>",
                subject="world",
                sender="from@example.com",
                recipients=["to@example.com", "cc@example.com"],
                sent_at=now,
                text_body="plain 2",
                html_body=None,
                fetched_at=now,
            ),
        ]

        with Session(engine) as s:
            inserted = EmailStore(s).bulk_insert(msgs)
            s.commit()
        assert inserted == 2

        with Session(engine) as s:
            cnt = s.scalar(
                select(func.count())
                .select_from(EmailMessage)
                .where(EmailMessage.account_id == acc_id)
            )
        assert cnt == 2

        # re-insert same batch should be idempotent (ON CONFLICT DO NOTHING)
        with Session(engine) as s:
            inserted2 = EmailStore(s).bulk_insert(msgs)
            s.commit()
        assert inserted2 == 0

        with Session(engine) as s:
            cnt = s.scalar(
                select(func.count())
                .select_from(EmailMessage)
                .where(EmailMessage.account_id == acc_id)
            )
        assert cnt == 2

        # recipients TEXT[] stored correctly
        with Session(engine) as s:
            rec = s.scalar(
                select(EmailMessage.recipients).where(
                    EmailMessage.account_id == acc_id, EmailMessage.uid == 1002
                )
            )
        assert rec == ["to@example.com", "cc@example.com"]
    finally:
        _cleanup(engine, acc_ids)


def test_update_checkpoint_persists(engine):
    acc_ids = []
    try:
        acc_id, _ = _create_account(engine, last_sync_uid=0)
        acc_ids.append(acc_id)

        new_uid = 999
        new_time = datetime.now(UTC)
        with Session(engine) as s:
            AccountStore(s).update_checkpoint(acc_id, new_uid, new_time)
            s.commit()

        with Session(engine) as s:
            row = s.get(Account, acc_id)
        assert row.last_sync_uid == new_uid
        assert row.last_sync_at is not None

        # verify get_enabled_accounts reflects updated uid
        with Session(engine) as s:
            accounts = AccountStore(s).get_enabled_accounts()
        matched = [a for a in accounts if a.id == acc_id]
        assert matched[0].last_sync_uid == new_uid
    finally:
        _cleanup(engine, acc_ids)


def test_full_flow_fetch_insert_checkpoint(engine):
    acc_ids = []
    try:
        acc_id, _ = _create_account(engine, last_sync_uid=10)
        acc_ids.append(acc_id)

        # simulate service: fetch enabled -> bulk insert -> checkpoint
        with Session(engine) as s:
            accounts = AccountStore(s).get_enabled_accounts()
        target = next(a for a in accounts if a.id == acc_id)
        assert target.last_sync_uid == 10

        now = datetime.now(UTC)
        msgs = [
            EmailMessage(account_id=acc_id, uid=11, subject="a", fetched_at=now),
            EmailMessage(account_id=acc_id, uid=12, subject="b", fetched_at=now),
            EmailMessage(account_id=acc_id, uid=13, subject="c", fetched_at=now),
        ]

        with Session(engine) as s:
            inserted = EmailStore(s).bulk_insert(msgs)
            assert inserted == 3
            max_uid = max(m.uid for m in msgs)
            AccountStore(s).update_checkpoint(acc_id, max_uid, now)
            s.commit()

        with Session(engine) as s:
            row = s.get(Account, acc_id)
        assert row.last_sync_uid == 13

        # re-run with same messages -> 0 inserted, checkpoint stays
        with Session(engine) as s:
            inserted2 = EmailStore(s).bulk_insert(msgs)
            s.commit()
        assert inserted2 == 0
    finally:
        _cleanup(engine, acc_ids)
