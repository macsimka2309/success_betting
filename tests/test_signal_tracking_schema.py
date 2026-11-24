"""Disposable database only; no API, Telegram or production access."""
import os

import psycopg
import pytest
from psycopg.types.json import Jsonb

from src.db import migrate
from tests.test_collect import conn  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable DB required')


def insert_signal(conn):
    mid = conn.execute('INSERT INTO live_alert_matches DEFAULT VALUES RETURNING alert_match_id').fetchone()[0]
    conn.execute("""INSERT INTO live_signal_tracking
      (signal_id,alert_match_id,feature_version,first_message_id,first_sent_at,
       confirmed_at,delivery_started_at,time_precision,first_context)
      VALUES(1,%s,1,123,now(),now(),now(),'delivery_interval',%s)""", (mid,Jsonb({})))
    return mid


def test_schema_constraints_and_empty_cutover(conn):
    assert conn.execute('SELECT tracking_enabled,reminders_enabled,activated_at FROM live_signal_config').fetchone() == (False,False,None)
    assert conn.execute('SELECT count(*) FROM live_signal_tracking').fetchone()[0] == 0
    insert_signal(conn)
    conn.execute('INSERT INTO live_signal_reminders(signal_id) VALUES(1)')
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute('INSERT INTO live_signal_reminders(signal_id) VALUES(1)')
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE live_signal_reminders SET state='sent'")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute('UPDATE live_signal_config SET tracking_enabled=true')
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute('UPDATE live_signal_tracking SET review_day=9')


def test_migration_preserves_legacy_and_can_reapply(conn):
    # These pre-existing public objects must remain identical across down/up.
    from tests.test_alert_dispatch import fb,run
    fb(conn);run(conn)
    names = ('live_alerts_sent','live_alert_outbox','live_match_statistics_75','alert_hit_rate')
    def snapshot():
        return {n: conn.execute('SELECT to_jsonb(t) FROM '+n+' t').fetchall() for n in names}
    before = snapshot()
    migration = next(m for m in migrate.discover() if m.version == '030')
    attempts=next(m for m in migrate.discover() if m.version=="034")
    source=next(m for m in migrate.discover() if m.version=="035")
    decisions=next(m for m in migrate.discover() if m.version=="036")
    migrate.revert(conn,decisions)
    migrate.revert(conn,source)
    migrate.revert(conn,attempts)
    identity=next(m for m in migrate.discover() if m.version=="033")
    migrate.revert(conn,identity)
    migrate.revert(conn,migration)
    migrate.apply(conn,migration)
    migrate.apply(conn,identity)
    migrate.apply(conn,attempts)
    migrate.apply(conn,source)
    migrate.apply(conn,decisions)
    assert snapshot() == before
    assert conn.execute('SELECT count(*) FROM live_signal_tracking').fetchone()[0] == 0
    assert migrate.run_up(conn) == []


def test_activation_is_durable_and_old_sends_never_enroll(conn):
    from datetime import datetime,timedelta,timezone
    from src.jobs.signal_tracking import configure,enrollment
    now=datetime(2026,11,19,tzinfo=timezone.utc)
    assert enrollment(conn,now) is None
    configure(conn,tracking=True,reminders=True,now=now)
    assert enrollment(conn,now-timedelta(seconds=1)) is None
    assert enrollment(conn,now)['version']==2
    configure(conn,tracking=False,reminders=False,now=now+timedelta(days=1))
    assert enrollment(conn,now+timedelta(days=1)) is None
    configure(conn,tracking=True,reminders=False,now=now+timedelta(days=2))
    assert conn.execute('SELECT activated_at FROM live_signal_config').fetchone()[0]==now
    assert conn.execute('SELECT count(*) FROM live_signal_tracking').fetchone()[0]==0


def test_source_policy_cutover_retires_old_queue_and_keeps_history(conn):
    from datetime import datetime, timezone
    from src.jobs.signal_goal_store import save_timeline
    from src.jobs.signal_goals import Timeline
    from src.jobs.signal_results import run_results
    from src.jobs.signal_tracking import configure

    migration=next(m for m in migrate.discover() if m.version=='035')
    mid=insert_signal(conn)
    now=datetime(2026,11,19,tzinfo=timezone.utc)
    save_timeline(conn,mid,'api_football','10',Timeline(status='FT'),now,{'archive':True})
    decisions=next(m for m in migrate.discover() if m.version=='036')
    migrate.revert(conn,decisions)
    migrate.revert(conn,migration)
    conn.execute('INSERT INTO live_signal_reminders(signal_id) VALUES(1)')
    migrate.apply(conn,migration)
    migrate.apply(conn,decisions)
    assert conn.execute("SELECT state,reason FROM live_signal_reminders").fetchone()==('cancelled','policy_changed')
    assert conn.execute("SELECT count(*) FROM live_signal_goal_observations").fetchone()==(1,)
    configure(conn,tracking=True,reminders=True,now=now)
    assert run_results(conn,now_fn=lambda:now,fonbet_loader=lambda *a,**k:pytest.fail('historical backfill'))=={}


def test_poll_decision_migration_recovers_due_and_retains_states(conn):
    from datetime import timedelta
    from tests.test_alert_dispatch import NOW
    from tests.test_signal_review_regressions import signal

    migration = next(m for m in migrate.discover() if m.version == '036')
    migrate.revert(conn, migration)
    for sid in range(1, 7):
        signal(conn, sid, review=True)
    later = NOW + timedelta(days=2)
    conn.execute("""UPDATE live_signal_tracking SET status='waiting_end',primary_due_at=%s,
      next_attempt_at=%s,deferred_reason='browser_busy' WHERE signal_id=1""", (NOW,later))
    conn.execute("""UPDATE live_signal_tracking SET status='review_due',review_day=1,
      primary_attempts=1,last_attempt_at=%s,next_attempt_at=%s WHERE signal_id=2""", (NOW,later))
    conn.execute("""UPDATE live_signal_tracking SET status='review_due',review_day=1,
      next_attempt_at=%s WHERE signal_id=3""", (later,))
    conn.execute("UPDATE live_signal_tracking SET status='reviewed' WHERE signal_id=4")
    conn.execute("""UPDATE live_signal_tracking SET primary_attempts=1,last_attempt_at=%s,
      next_attempt_at=%s WHERE signal_id=5""", (NOW,NOW+timedelta(minutes=20)))
    conn.execute("""UPDATE live_signal_tracking SET primary_attempts=1,timeline_complete=true,
      next_attempt_at=%s WHERE signal_id=6""", (later,))
    conn.execute("""INSERT INTO live_signal_reminders(signal_id,state)
      VALUES(1,'waiting'),(3,'uncertain'),(4,'cancelled')""")
    conn.execute("""INSERT INTO live_signal_reminders(signal_id,state,telegram_message_id,sent_at)
      VALUES(2,'sent',99,%s)""",(NOW,))
    migrate.apply(conn, migration)
    due = conn.execute("""SELECT result_due_at,result_due_from_admission
      FROM live_signal_tracking ORDER BY signal_id""").fetchall()
    assert due == [(NOW,False),(NOW+timedelta(days=1),False),(later,True),(None,False),
                   (NOW+timedelta(minutes=5),False),(NOW+timedelta(days=1),False)]
    assert conn.execute("""SELECT state,first_decision FROM live_signal_reminders
      ORDER BY signal_id""").fetchall() == [
        ('waiting',None),('sent',None),('uncertain',None),('cancelled',None)]
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE live_signal_reminders SET first_decision='{}' WHERE signal_id=1")
