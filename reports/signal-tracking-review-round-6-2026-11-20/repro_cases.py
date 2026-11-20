"""Additional review checks for Fonbet goal identity and API reminder provenance."""
from datetime import datetime, timedelta
import os

import pytest

from src.jobs.alert_dispatch import rows
from src.jobs.collect import collection_run
from src.jobs.live_context import record_api_context
from src.jobs.live_statistics import collect_statistics
from src.jobs.signal_reminder_history import available_polls
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_tracking import configure
from tests.test_alert_dispatch import NOW, run
from tests.test_collect import conn, make_client  # noqa: F401
from tests.test_live_alerts import live_item
from tests.test_live_statistics import body
from tests.test_signal_reminders import archive_api_poll
from tests.test_signal_results import goal_at, read_goal_snapshot, seed

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable DB required')


def test_known_fonbet_goal_keeps_its_id_when_same_minute_penalty_is_added(conn):
    seed(conn)
    at = NOW + timedelta(minutes=20)
    open_play = dict(goal_at(90, 2, 1), detail='Гол')
    assert read_goal_snapshot(conn, at, [open_play]) == {'waiting_end': 1}
    known_id, = conn.execute('SELECT goal_id FROM live_signal_goals').fetchone()

    final = [dict(goal_at(90, 2, 1), detail='Пенальти забит'),
             dict(open_play, score_after_home=2)]
    assert read_goal_snapshot(conn, at + timedelta(minutes=1), final, finished=True) == {'preliminary': 1}
    by_type = {
        detail: (goal_id, score)
        for detail, goal_id, score in conn.execute("""
            SELECT source_detail, goal_id, score_after_home FROM live_signal_goals WHERE active
        """)
    }
    assert by_type['Гол'] == (known_id, 2) and by_type['Пенальти забит'][0] != known_id, by_type


def test_ambiguous_fonbet_minute_correction_does_not_add_third_active_goal(conn):
    seed(conn)
    at = NOW + timedelta(minutes=20)
    original = [dict(goal_at(90, 2, score), detail='Гол') for score in (1, 2)]
    assert read_goal_snapshot(conn, at, original) == {'waiting_end': 1}

    corrected = [dict(goal_at(88, None, 1), detail='Гол'), original[1]]
    read_goal_snapshot(conn, at + timedelta(minutes=1), corrected, finished=True)
    active = conn.execute("""
        SELECT goal_id, minute, added_minute, score_after_home
        FROM live_signal_goals WHERE active ORDER BY goal_id
    """).fetchall()
    assert len(active) == 2, active


def test_duplicate_api_context_cannot_authorize_reminder_via_retained_snapshot(conn, tmp_path):
    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 74, 6)
    assert run(conn)['sent'] == 1

    client = make_client(tmp_path, [
        {'response': [live_item(1, minute=73)]},
        {'response': [live_item(1, minute=74), live_item(1, minute=75)]},
        body(6, 0),
    ])
    fake_now = client.now
    start = (NOW + timedelta(minutes=1)).timestamp()
    client.now = lambda: start + fake_now()
    with collection_run(conn, client, 'live_alerts') as collection:
        initial = client.get('/fixtures', {'live': 'all'}, use_cache=False)
        context = client.last_observation
        record_api_context(conn, initial['response'], context)
        client.sleep(24)
        collect_statistics(client, conn, initial['response'], context, collection.run_id)
    assert client.requests_used == 3
    at = datetime.fromtimestamp(client.last_observation.observed_at, NOW.tzinfo)
    observation_id = client.last_observation.observation_id
    context_id, latest, poll_id = conn.execute("""
        SELECT s.context_observation_id, s.elapsed, c.poll_id
        FROM live_match_statistics_75 s
        JOIN live_alert_context c ON c.source='api_football' AND c.source_event_id=s.fixture_id::text
        WHERE s.observation_id=%s
    """, (observation_id,)).fetchone()
    assert latest == 75 and poll_id == str(context_id)
    job, = rows(conn, """
        SELECT t.*, r.first_decision FROM live_signal_tracking t
        JOIN live_signal_reminders r USING (signal_id)
    """)
    assert available_polls(conn, job, at) == []

    sent = []
    dispatch_reminders(conn, lambda *args: sent.append(args) or 999, now_fn=lambda: at)
    state = conn.execute('SELECT state, first_decision FROM live_signal_reminders').fetchone()
    assert (state, len(sent)) == (('waiting', None), 0), (state, len(sent))
