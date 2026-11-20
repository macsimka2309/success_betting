"""Verify goal identity across canonical merges and incomplete source updates."""
from datetime import timedelta
import os

import pytest

from src.jobs.signal_goal_store import save_timeline
from src.jobs.signal_goals import Timeline
from tests.test_alert_dispatch import NOW
from tests.test_collect import conn  # noqa: F401
from tests.test_signal_results import goal_at, read_goal_snapshot, seed

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable DB required')


def test_replay_after_alias_merge_keeps_original_observation_and_goal_revision(conn):
    canonical_id, = conn.execute("SELECT live_alert_identity('api_football','1')").fetchone()
    fonbet_id, = conn.execute("SELECT live_alert_identity('fonbet','123')").fetchone()
    assert canonical_id < fonbet_id

    def observe(match_id):
        goal = dict(goal_at(84, None, 1), detail='Гол')
        timeline = Timeline(goals=[goal], status='2H')
        with conn.transaction():
            return save_timeline(conn, match_id, 'fonbet', '123', timeline, NOW, {'structure_valid': True})

    observation_id = observe(fonbet_id)
    before = conn.execute('SELECT goal_id,observation_id,revision FROM live_signal_goals').fetchone()
    assert conn.execute("SELECT live_alert_link('123',1)").fetchone() == (canonical_id,)
    replay_id = observe(canonical_id)
    after = conn.execute('SELECT goal_id,observation_id,revision FROM live_signal_goals').fetchone()
    observations, = conn.execute('SELECT count(*) FROM live_signal_goal_observations').fetchone()
    versions, = conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone()
    assert (replay_id, after, observations, versions) == (observation_id, before, 1, 1)


def test_incomplete_type_conflict_cannot_transfer_penalty_id_to_open_play(conn):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    penalty = dict(goal_at(90, 2, 1), detail='Пенальти забит')
    assert read_goal_snapshot(conn, first, [penalty]) == {'waiting_end': 1}
    penalty_id, = conn.execute('SELECT goal_id FROM live_signal_goals').fetchone()

    conflicting = dict(penalty, detail='Гол')
    assert read_goal_snapshot(conn, first + timedelta(minutes=1), [conflicting], valid=False) == {
        'waiting_end': 1,
    }
    after_partial = conn.execute('SELECT source_detail FROM live_signal_goals WHERE goal_id=%s',
                                 (penalty_id,)).fetchone()

    final = [conflicting, dict(penalty, score_after_home=2)]
    assert read_goal_snapshot(conn, first + timedelta(minutes=2), final, finished=True) == {'preliminary': 1}
    after_final = conn.execute("""
        SELECT goal_id,source_detail,score_after_home
        FROM live_signal_goals WHERE active ORDER BY score_after_home
    """).fetchall()
    assert after_partial == ('Пенальти забит',) and after_final[1] == (penalty_id, 'Пенальти забит', 2), (
        penalty_id, after_partial, after_final,
    )
    assert len(after_final) == 2 and after_final[0][0] != penalty_id
