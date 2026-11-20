"""Expected behavior in three boundary cases. Use a disposable PostgreSQL only."""
from datetime import timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.jobs.signal_goals import Timeline, load_fonbet_timeline
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_results import run_results
from src.jobs.signal_tracking import configure
from tests.test_alert_dispatch import NOW, fb, run
from tests.test_collect import conn  # noqa: F401
from tests.test_live_source_matching import ROW
from tests.test_signal_results import live_context, seed
from tests.test_signal_review_regressions import signal
from tools.fonbet_live.store import build_sql

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable DB required')
FIXTURES = Path(__file__).resolve().parents[2] / 'tests/fixtures/fonbet'


def import_statistics(at, minute, home_shots, conn):
    requested = (at - timedelta(seconds=2)).isoformat()
    observed_context = (at - timedelta(seconds=1)).isoformat()
    row = dict(ROW, elapsed=minute, clock_text=f'{minute}:00',
               goals_home=0, goals_away=0, ht_home=0, ht_away=0,
               shots_on_target_2h_home=home_shots, shots_on_target_2h_away=0,
               run_id=str(uuid4()), observation_id=str(uuid4()),
               discovered_at=requested, requested_at=requested,
               context_observed_at=observed_context, observed_at=at.isoformat())
    context = dict(fonbet_event_id='123', clock_text=row['clock_text'],
                   period_text='2-й тайм', observed_at=observed_context)
    export = dict(run_id=row['run_id'], started_at=requested, finished_at=at.isoformat(),
                  status='complete', summary={}, observations=[row], contexts=[context])
    conn.execute(build_sql(export))


def test_next_negative_poll_cannot_be_erased_by_later_positive_poll(conn):
    configure(conn, tracking=True, reminders=True, now=NOW)
    import_statistics(NOW, 67, 6, conn)
    assert run(conn)['sent'] == 1
    import_statistics(NOW + timedelta(minutes=5), 70, 5, conn)
    import_statistics(NOW + timedelta(minutes=10), 75, 6, conn)
    assert conn.execute('SELECT count(*) FROM fonbet_live_observations').fetchone() == (3,)
    sent = []
    dispatch_reminders(conn, lambda *a: sent.append(a) or 999,
                       now_fn=lambda: NOW + timedelta(minutes=10, seconds=1))
    state = conn.execute('SELECT state,reason FROM live_signal_reminders').fetchone()
    assert (state, len(sent)) == (('cancelled', 'trigger_cleared'), 0)


def test_second_stoppage_goal_from_integral_live_timeline_reaches_projection(conn):
    seed(conn)
    html = (FIXTURES / 'timeline-seven-goals.html').read_text()
    html = html.replace("79'", "90+2'").replace("84'", "90+6'")
    data = dict(html=html, source_event_id='123', clock_text='90+6', period_text='2-й тайм')

    def loader(url, **kwargs):
        return load_fonbet_timeline(url, runner=lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=json.dumps(data)), **kwargs)

    tick = NOW.replace(second=45) + timedelta(minutes=9)
    live_context(conn, '2H', 89, tick - timedelta(seconds=3))
    assert run_results(conn, fonbet_loader=loader, now_fn=lambda: tick,
                       scheduled=True) == {'waiting_end': 1}
    goals = conn.execute("""SELECT added_minute FROM live_signal_goals_selected
       WHERE minute=90 AND relation='after' ORDER BY added_minute""").fetchall()
    pending = conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone()[0]
    assert (goals, pending) == ([(2,), (6,)], 0)


def test_post_match_due_does_not_keep_losing_to_concurrent_late_slots(conn):
    first_tick = NOW.replace(second=45) + timedelta(days=1)
    configure(conn, tracking=True, reminders=False, now=NOW)
    fb(conn)
    signal(conn, 1, when=first_tick-timedelta(minutes=24), eid='123')
    fb(conn, eid='124')
    signal(conn, 2, review=True, eid='124')
    calls = []

    def loader(url, **kwargs):
        calls.append((url.rsplit('/', 1)[1], kwargs['regulation_end_confirmed']))
        return Timeline(status='2H', elapsed=90, added_minute=5, period='regulation_2h'), None, {}

    for offset in range(16):
        tick = first_tick + timedelta(minutes=offset)
        live_context(conn, '2H', 89, tick - timedelta(seconds=3))
        run_results(conn, fonbet_loader=loader, now_fn=lambda: tick,
                    scheduled=True, browser_limit=1)
    review = conn.execute("""SELECT review_day,deferred_reason FROM live_signal_tracking
      WHERE signal_id=2""").fetchone()
    assert ('124', True) in calls, (calls, review)
