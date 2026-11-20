"""Boundary cases for goal identity and historical API association."""
from datetime import timedelta
import os

import pytest

from src.jobs.signal_reminder_history import available_polls
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_tracking import configure
from src.jobs.alert_dispatch import rows
from tests.test_alert_dispatch import NOW, run
from tests.test_collect import conn  # noqa: F401
from tests.test_signal_reminders import archive_api_poll
from tests.test_signal_results import goal_at, read_goal_snapshot, seed

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable DB required')


def test_same_minute_goals_keep_their_ids_after_an_earlier_goal_is_inserted(conn):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    penalty = dict(goal_at(90,2,1),detail='Пенальти забит')
    open_play = dict(goal_at(90,2,2),detail='Гол')
    assert read_goal_snapshot(conn,first,[penalty,open_play]) == {'waiting_end':1}

    before = conn.execute("""SELECT v.goal_id,v.score_after_home,g.source_detail FROM live_signal_goals_selected v
        JOIN live_signal_goals g USING(goal_id) WHERE v.minute=90 ORDER BY v.score_after_home""").fetchall()
    assert len(before) == 2
    first_id, second_id = before[0][0], before[1][0]
    final = [dict(goal_at(88,None,1),detail='Гол'),
             dict(penalty,score_after_home=2),dict(open_play,score_after_home=3)]
    assert read_goal_snapshot(conn,first+timedelta(minutes=1),final,finished=True) == {'preliminary':1}
    after = conn.execute("""SELECT v.goal_id,v.score_after_home,g.source_detail FROM live_signal_goals_selected v
        JOIN live_signal_goals g USING(goal_id) WHERE v.minute=90 ORDER BY v.score_after_home""").fetchall()
    assert after == [(first_id,2,'Пенальти забит'),(second_id,3,'Гол')]


def test_ambiguous_refreshed_api_context_cannot_fall_back_to_older_context(conn):
    configure(conn,tracking=True,reminders=True,now=NOW)
    archive_api_poll(conn,NOW,74,6)
    assert run(conn)['sent'] == 1
    at = NOW + timedelta(minutes=1)
    observation_id = archive_api_poll(conn,at,73,6)
    context_id = conn.execute("""DELETE FROM live_api_reminder_observations WHERE observation_id=%s
        RETURNING context_observation_id""",(int(observation_id),)).fetchone()[0]
    assert conn.execute('SELECT elapsed FROM live_match_statistics_75').fetchone() == (74,)

    # An earlier valid /fixtures answer triggered a refresh after 24 seconds.
    older_context_id = conn.execute("""INSERT INTO api_observations(run_id,endpoint,params,requested_at,observed_at,payload)
        SELECT run_id,endpoint,params,requested_at-interval '24 seconds',observed_at-interval '24 seconds',
            jsonb_set(payload,'{response,0,fixture,status,elapsed}','72')
        FROM api_observations WHERE observation_id=%s RETURNING observation_id""",(context_id,)).fetchone()[0]
    # The newer response, whose last row was used by the collector, carries this
    # fixture twice. It cannot prove the archived statistics binding.
    conn.execute("""UPDATE api_observations SET payload=jsonb_set(payload,'{response}',
        jsonb_build_array(jsonb_set(payload->'response'->0,'{fixture,status,elapsed}','72'),
            payload->'response'->0)) WHERE observation_id=%s""",(context_id,))
    job = rows(conn,'''SELECT t.*,r.first_decision FROM live_signal_tracking t
        JOIN live_signal_reminders r USING(signal_id)''')[0]
    polls = available_polls(conn,job,at)
    sent = []
    dispatch_reminders(conn,lambda *a:sent.append(a) or 999,now_fn=lambda:at)
    state = conn.execute('SELECT state,first_decision FROM live_signal_reminders').fetchone()
    assert (state,len(sent)) == (('waiting',None),0), (state,older_context_id,[p['statistics_poll_id'] for p in polls])
