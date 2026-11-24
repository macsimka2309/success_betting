"""First-delivery transaction and enrollment boundaries, all transports mocked."""
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from src.jobs import alert_dispatch as ad
from src.jobs.signal_tracking import DeliveryReceipt,configure
from tests.test_collect import conn  # noqa: F401
from tests.test_alert_dispatch import fb,run,NOW

pytestmark=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='disposable DB required')


def test_new_confirmed_signal_enrolls_once(conn):
    configure(conn,tracking=True,reminders=True,now=NOW-timedelta(seconds=1))
    fb(conn)
    assert run(conn,lambda *a:DeliveryReceipt(17,NOW-timedelta(seconds=1)))['sent']==1
    assert conn.execute('SELECT first_message_id,time_precision FROM live_signal_tracking').fetchone()==(17,'telegram_second')
    assert conn.execute('SELECT state FROM live_signal_reminders').fetchone()==('waiting',)
    run(conn,lambda *a:pytest.fail('duplicate delivery'))
    assert conn.execute('SELECT count(*) FROM live_signal_tracking').fetchone()[0]==1


def test_no_retroactive_enrollment(conn):
    fb(conn);run(conn)
    configure(conn,tracking=True,reminders=True,now=NOW)
    run(conn)
    assert conn.execute('SELECT count(*) FROM live_signal_tracking').fetchone()[0]==0


def test_uncertain_does_not_invent_tracking(conn):
    configure(conn,tracking=True,reminders=True,now=NOW)
    fb(conn)
    def send(*a):raise ad.DeliveryFailure()
    run(conn,send)
    assert conn.execute('SELECT count(*) FROM live_signal_tracking').fetchone()[0]==0


def test_commit_failure_rolls_back_tracking_and_recovers_uncertain(conn,monkeypatch):
    configure(conn,tracking=True,reminders=True,now=NOW)
    fb(conn)
    def broken(*a):raise RuntimeError('simulated post-send transaction failure')
    monkeypatch.setattr(ad,'record_legacy',broken)
    with pytest.raises(RuntimeError):run(conn)
    assert conn.execute('SELECT count(*) FROM live_signal_tracking').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM live_signal_reminders').fetchone()[0]==0
    assert conn.execute('SELECT state FROM live_alert_outbox').fetchone()==('sending',)
    conn.execute("UPDATE live_alert_outbox SET lease_until='2000-01-01'")
    run(conn,lambda *a:pytest.fail('uncertain replay'))
    assert conn.execute('SELECT state FROM live_alert_outbox').fetchone()==('uncertain',)


def test_missing_telegram_date_preserves_interval(conn):
    configure(conn,tracking=False,reminders=True,now=NOW)
    fb(conn);run(conn)
    assert conn.execute('SELECT time_precision,goals_enabled FROM live_signal_tracking').fetchone()==('delivery_interval',False)


def test_transport_receipt_keeps_telegram_timestamp():
    sender=ad.telegram_sender('test',1,with_receipt=True,runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout='{"ok":true,"result":{"message_id":77,"date":1795046400}}'))
    receipt=sender('test',None)
    assert receipt.message_id==77 and int(receipt.sent_at.timestamp())==1795046400
