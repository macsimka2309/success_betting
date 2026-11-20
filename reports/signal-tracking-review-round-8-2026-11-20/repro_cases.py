"""Verify terminal API context identity and Fonbet reconciliation invariants."""
from datetime import timedelta
import os

import pytest

from src.jobs.signal_goal_store import save_timeline
from src.jobs.signal_goals import Timeline
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_tracking import configure
from tests.test_alert_dispatch import NOW, run
from tests.test_collect import conn  # noqa: F401
from tests.test_live_alerts import live_item
from tests.test_signal_reminders import archive_api_poll, archive_live_response
from tests.test_signal_results import goal_at, read_goal_snapshot, seed

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable DB required')


@pytest.mark.parametrize('minute,status', [(76, '2H'), (90, 'FT')])
def test_current_api_context_with_different_teams_cannot_close_reminder(conn, tmp_path, minute, status):
    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 72, 6)
    assert run(conn)['sent'] == 1
    first_id = archive_api_poll(conn, NOW + timedelta(minutes=1), 73, 6)

    item = live_item(1, minute=minute, status=status)
    item['teams'] = {'home': {'id': 30, 'name': 'Team C'}, 'away': {'id': 40, 'name': 'Team D'}}
    at = NOW + timedelta(minutes=2)
    archive_live_response(conn, tmp_path, at, [item])
    dispatch_reminders(conn, lambda *args: pytest.fail('incompatible team identity'), now_fn=lambda: at)
    state = conn.execute("""
        SELECT state,reason,first_decision->>'observation_id' FROM live_signal_reminders
    """).fetchone()
    assert state == ('waiting', None, first_id), state


def test_known_same_minute_penalty_is_updated_when_neighbor_type_conflicts(conn):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    penalty = dict(goal_at(90, 2, 1), detail='Пенальти забит')
    open_play = dict(goal_at(90, 2, 2), detail='Гол')
    assert read_goal_snapshot(conn, first, [penalty, open_play]) == {'waiting_end': 1}
    penalty_id, = conn.execute("""
        SELECT goal_id FROM live_signal_goals WHERE source_detail='Пенальти забит'
    """).fetchone()

    final = [
        dict(goal_at(88, None, 0), detail='Гол', credited_side='away', score_after_away=1),
        dict(penalty, score_after_away=1),
        dict(open_play, detail='Автогол', score_after_away=1),
    ]
    read_goal_snapshot(conn, first + timedelta(minutes=1), final, finished=True)
    row = conn.execute("""
        SELECT goal_id,score_after_home,score_after_away
        FROM live_signal_goals WHERE active AND source_detail='Пенальти забит'
    """).fetchone()
    assert row == (penalty_id, 1, 1), (
        row,
        conn.execute('SELECT candidate_goal_ids,payload FROM live_signal_goal_pending').fetchall(),
    )


def test_replayed_live_receipt_does_not_undo_confirmed_part_of_newer_review(conn):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    original = [
        dict(goal_at(70, None, 1), detail='Гол'),
        dict(goal_at(90, 2, 2), detail='Гол'),
        dict(goal_at(90, 2, 3), detail='Гол'),
    ]
    assert read_goal_snapshot(conn, first, original) == {'waiting_end': 1}
    old_oid, match_id, source, event_id, old_at, snapshot = conn.execute("""
        SELECT observation_id,alert_match_id,source,source_event_id,observed_at,payload
        FROM live_signal_goal_observations
    """).fetchone()
    corrected = [
        dict(goal_at(69, None, 0), detail='Гол', credited_side='away', score_after_away=1),
        dict(original[0], score_after_away=1),
        dict(original[1], minute=88, added_minute=None, score_after_away=1),
        dict(original[2], score_after_away=1),
    ]
    read_goal_snapshot(conn, first + timedelta(minutes=1), corrected, finished=True)
    assert conn.execute("""
        SELECT complete,incomplete_reason FROM live_signal_goal_observations ORDER BY observation_id DESC LIMIT 1
    """).fetchone() == (False, 'identity_ambiguous')

    def earlier_goal():
        return conn.execute("""
            SELECT goal_id,score_after_away,observation_id,revision
            FROM live_signal_goals WHERE active AND minute=70
        """).fetchone()

    corrected_row = earlier_goal()
    assert corrected_row[1] == 1
    with conn.transaction():
        replayed = save_timeline(conn, match_id, source, event_id, Timeline(**snapshot['timeline']),
                                 old_at, snapshot['payload'])
    assert replayed == old_oid
    assert earlier_goal() == corrected_row, (corrected_row, earlier_goal())
