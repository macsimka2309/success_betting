"""Boundary regressions for reminder cutover and goal identity."""
from datetime import timedelta
import os

import pytest

from src.db import migrate
from src.jobs.signal_goals import Timeline
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_results import run_results
from src.jobs.signal_tracking import configure
from tests.test_alert_dispatch import NOW, run
from tests.test_collect import conn  # noqa: F401
from tests.test_signal_reminders import archive_api_poll
from tests.test_signal_results import seed

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable DB required')


def goal_at(minute, added, score):
    return {
        'period':'regulation_2h', 'credited_side':'home', 'minute':minute,
        'added_minute':added, 'score_after_home':score, 'score_after_away':0,
    }


def read_result(conn, at, goals, *, valid=True, finished=False):
    status = 'FT' if finished else '2H'
    timeline = Timeline(goals=goals, complete=finished, status=status, elapsed=90,
                        added_minute=6, period='regulation_2h',
                        reason='timeline_incomplete' if not valid else None)
    payload = {'structure_valid':valid, 'score':[len(goals),0]}
    return run_results(conn, now_fn=lambda:at,
                       fonbet_loader=lambda *a,**kw:(timeline,None,payload))


def selected(conn):
    return conn.execute("""SELECT goal_id,minute,added_minute,score_after_home
      FROM live_signal_goals_selected ORDER BY score_after_home""").fetchall()


def test_partial_same_timestamp_with_a_different_score_cannot_overwrite_known_goal(conn):
    seed(conn)
    first = NOW+timedelta(minutes=20)
    assert read_result(conn,first,[goal_at(90,2,1)])=={'waiting_end':1}
    known = selected(conn)
    assert len(known)==1
    assert read_result(conn,first+timedelta(minutes=1),[goal_at(90,2,2)],valid=False)=={'waiting_end':1}
    pending = conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone()[0]
    assert (selected(conn),pending)==(known,1)


def test_complete_timeline_correction_preserves_both_goal_identities_at_same_minute(conn):
    seed(conn)
    first = NOW+timedelta(minutes=20)
    original = [goal_at(86,None,1),goal_at(90,2,2)]
    assert read_result(conn,first,original)=={'waiting_end':1}
    known = selected(conn)
    assert len(known)==2
    final = [goal_at(90,2,1),goal_at(90,2,2)]
    assert read_result(conn,first+timedelta(minutes=1),final,finished=True)=={'preliminary':1}
    assert selected(conn)==[(known[0][0],90,2,1),(known[1][0],90,2,2)]


def test_cutover_retains_first_api_poll_when_archived_pairs_are_unique(conn):
    configure(conn,tracking=True,reminders=True,now=NOW)
    archive_api_poll(conn,NOW,67,6)
    assert run(conn)['sent']==1
    archive_api_poll(conn,NOW+timedelta(minutes=5),70,5)
    latest = NOW+timedelta(minutes=10)
    archive_api_poll(conn,latest,75,6)

    # Simulate storage immediately before 036. Each API run contains one
    # context and one statistical response, and the raw archive is preserved.
    migration = next(m for m in migrate.discover() if m.version=='036')
    migrate.revert(conn,migration)
    assert conn.execute("""SELECT count(*) FROM (
        SELECT run_id FROM api_observations
        GROUP BY run_id HAVING count(*)=2 AND count(*) FILTER (WHERE endpoint='/fixtures')=1
        AND count(*) FILTER (WHERE endpoint='/fixtures/statistics')=1
    ) paired""").fetchone()==(3,)
    migrate.apply(conn,migration)
    sent = []
    dispatch_reminders(conn,lambda *a:sent.append(a) or 999,now_fn=lambda:latest)
    reminder = conn.execute('SELECT state,reason,first_decision FROM live_signal_reminders').fetchone()
    assert (reminder[:2],reminder[2]['minute'],len(sent))==(('cancelled','trigger_cleared'),70,0)
