"""Stale Fonbet status must not supersede later evidence of live play."""
from datetime import timedelta
import os

import pytest

from src.jobs.signal_goals import Timeline
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_results import run_results
from tests.test_alert_dispatch import NOW
from tests.test_collect import conn  # noqa: F401
from tests.test_signal_results import goal_at, live_context, read_goal_snapshot, seed

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable DB required')


def seed_tracked_match(conn):
    match_id = seed(conn)
    conn.execute("INSERT INTO live_alert_aliases VALUES('fonbet','123',%s)", (match_id,))
    conn.execute('UPDATE live_signal_config SET reminders_enabled=true')
    conn.execute('INSERT INTO live_signal_reminders(signal_id) VALUES(1)')


@pytest.mark.parametrize('terminal', ['FT', 'ABD'])
def test_delayed_terminal_browser_reply_cannot_close_newer_live_match(conn, terminal):
    seed_tracked_match(conn)
    tick = NOW + timedelta(minutes=10)
    assert read_goal_snapshot(conn, tick, [goal_at(89, None, 1)]) == {'waiting_end': 1}

    def delayed(*args, **kwargs):
        return Timeline(status=terminal), (tick - timedelta(seconds=30)).isoformat(), {}

    run_results(conn, fonbet_loader=delayed, now_fn=lambda: tick + timedelta(minutes=1))
    assert conn.execute("""
        SELECT t.status,t.regulation_end_confirmed_at,r.state
        FROM live_signal_tracking t JOIN live_signal_reminders r USING (signal_id)
    """).fetchone() == ('waiting_end', None, 'waiting')


@pytest.mark.parametrize('consumer', ['dispatcher', 'results'])
def test_delayed_terminal_context_cannot_close_match_after_later_browser_live(conn, consumer):
    seed_tracked_match(conn)
    tick = NOW + timedelta(minutes=10)
    minute = 74 if consumer == 'dispatcher' else 89
    assert run_results(conn, now_fn=lambda: tick, fonbet_loader=lambda *a, **k: (
        Timeline(status='2H', period='regulation_2h', elapsed=minute), tick.isoformat(), {},
    )) == {'waiting_end': 1}
    live_context(conn, 'FT', 90, tick - timedelta(seconds=20))
    now = lambda: tick + timedelta(minutes=1)
    if consumer == 'dispatcher':
        dispatch_reminders(conn, lambda *args: pytest.fail('unexpected delivery'), now_fn=now)
    else:
        run_results(conn, now_fn=now, fonbet_loader=lambda *a, **k: (
            Timeline(status='2H', period='regulation_2h', elapsed=90), None, {},
        ))
    assert conn.execute("""
        SELECT t.status,t.regulation_end_confirmed_at,r.state
        FROM live_signal_tracking t JOIN live_signal_reminders r USING (signal_id)
    """).fetchone() == ('waiting_end', None, 'waiting')


def test_delayed_browser_finish_cannot_override_more_recent_regular_live_context(conn):
    seed_tracked_match(conn)
    tick = NOW + timedelta(minutes=10)
    live_context(conn, '2H', 89, tick - timedelta(seconds=5))
    run_results(conn, now_fn=lambda: tick, fonbet_loader=lambda *args, **kwargs: (
        Timeline(status='FT'), (tick - timedelta(seconds=30)).isoformat(), {},
    ))
    assert conn.execute("""
        SELECT t.status,t.regulation_end_confirmed_at,r.state
        FROM live_signal_tracking t JOIN live_signal_reminders r USING (signal_id)
    """).fetchone() == ('waiting_end', None, 'waiting')


def test_delayed_void_cannot_erase_newer_post_match_evidence(conn):
    seed_tracked_match(conn)
    ended = NOW + timedelta(minutes=10)
    first_goal = goal_at(87, None, 1)
    assert run_results(conn, now_fn=lambda: ended, fonbet_loader=lambda *a, **k: (
        Timeline(goals=[first_goal], status='FT'), ended.isoformat(), {},
    )) == {'preliminary': 1}
    corrected_at = ended + timedelta(minutes=5)
    assert read_goal_snapshot(conn, corrected_at, [first_goal, goal_at(90, 3, 2)],
                              finished=True) == {'preliminary': 1}
    review_at = ended + timedelta(days=1)
    run_results(conn, now_fn=lambda: review_at, fonbet_loader=lambda *a, **k: (
        Timeline(status='ABD'), (ended + timedelta(minutes=2)).isoformat(), {},
    ))
    assert conn.execute("""
        SELECT status,timeline_complete,regulation_end_confirmed_at,review_due_at
        FROM live_signal_tracking
    """).fetchone() == ('review_due', True, ended, review_at)
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone() == (2,)
