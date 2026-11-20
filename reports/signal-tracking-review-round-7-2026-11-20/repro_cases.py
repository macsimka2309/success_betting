"""Review of same-run API context refreshes and cross-minute Fonbet matching."""
from datetime import datetime, timedelta
import os

import pytest

from src.jobs.collect import collection_run
from src.jobs.live_context import record_api_context
from src.jobs.live_statistics import collect_statistics
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_tracking import configure
from tests.test_alert_dispatch import NOW, run
from tests.test_collect import conn, make_client  # noqa: F401
from tests.test_live_alerts import live_item
from tests.test_live_statistics import body
from tests.test_signal_reminders import archive_api_poll
from tests.test_signal_results import goal_at, read_goal_snapshot, seed

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable DB required')


@pytest.mark.parametrize('last_minute', [75, 76])
def test_ambiguous_refresh_cannot_send_or_cancel_an_api_reminder(conn, tmp_path, last_minute):
    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 72, 6)
    assert run(conn)['sent'] == 1

    other_match = live_item(2, minute=73)
    other_match['teams'] = {'home': {'id': 30, 'name': 'Team C'}, 'away': {'id': 40, 'name': 'Team D'}}
    other_refreshed = dict(live_item(2, minute=74), teams=other_match['teams'])
    other_statistics = body(0, 0)
    for team_id, stat in zip((30, 40), other_statistics['response']):
        stat['team']['id'] = team_id
    matches = [live_item(1, minute=73), other_match]
    refreshed = [live_item(1, minute=74), live_item(1, minute=last_minute), other_refreshed]
    client = make_client(tmp_path, [{'response': matches}, body(6, 0), {'response': refreshed}, other_statistics])
    fake_now = client.now
    start = (NOW + timedelta(minutes=1)).timestamp()
    client.now = lambda: start + fake_now()
    scripted_transport = client.transport

    def transport(*args):
        response = scripted_transport(*args)
        # The first match's statistics are accepted within 30 seconds, but the
        # next match's iteration needs to refresh the live list.
        if len(scripted_transport.calls) == 2:
            client.sleep(24)
        return response

    client.transport = transport
    with collection_run(conn, client, 'live_alerts') as collection:
        initial = client.get('/fixtures', {'live': 'all'}, use_cache=False)
        context = client.last_observation
        record_api_context(conn, initial['response'], context)
        collect_statistics(client, conn, initial['response'], context, collection.run_id)

    assert client.requests_used == 4
    assert conn.execute('SELECT elapsed FROM live_match_statistics_75 WHERE fixture_id=1').fetchone() == (73,)
    assert conn.execute("""
        SELECT elapsed FROM live_alert_context WHERE source='api_football' AND source_event_id='1'
    """).fetchone() == (last_minute,)
    at = datetime.fromtimestamp(client.last_observation.observed_at, NOW.tzinfo)
    sent = []
    dispatch_reminders(conn, lambda *args: sent.append(args) or 999, now_fn=lambda: at)
    state, reason, first_minute = conn.execute("""
        SELECT state,reason,first_decision->>'minute' FROM live_signal_reminders
    """).fetchone()
    assert first_minute == '73'
    assert (state, reason, len(sent)) == ('waiting', None, 0), (state, reason, len(sent))


def test_disputed_late_goals_do_not_absorb_an_earlier_same_clock_match(conn):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    original = [
        dict(goal_at(70, None, 1), detail='Гол'),
        dict(goal_at(90, 2, 2), detail='Гол'),
        dict(goal_at(90, 2, 3), detail='Гол'),
    ]
    assert read_goal_snapshot(conn, first, original) == {'waiting_end': 1}
    earlier_id, = conn.execute('SELECT goal_id FROM live_signal_goals WHERE minute=70').fetchone()

    final = [
        dict(goal_at(69, None, 0), detail='Гол', credited_side='away', score_after_away=1),
        dict(original[0], score_after_away=1),
        dict(original[1], minute=88, added_minute=None, score_after_away=1),
        dict(original[2], score_after_away=1),
    ]
    read_goal_snapshot(conn, first + timedelta(minutes=1), final, finished=True)
    earlier = conn.execute("""
        SELECT goal_id, score_after_home, score_after_away
        FROM live_signal_goals WHERE minute=70 AND active
    """).fetchall()
    pending = conn.execute("""
        SELECT payload->>'minute', candidate_goal_ids
        FROM live_signal_goal_pending WHERE payload->>'minute'='70'
    """).fetchall()
    assert (earlier, pending) == ([(earlier_id, 1, 1)], []), (earlier, pending)
