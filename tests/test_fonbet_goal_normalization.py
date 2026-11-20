"""Raw shots + goals scored before the snapshot, never the final score."""
import os
import pytest
from tests.test_collect import conn  # noqa: F401
from tests.test_live_source_matching import add_fonbet,seed_api,reconcile_event,add_api_statistics
from tests.test_alert_dispatch import fb,run
from store import build_sql

pytestmark=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='disposable database required')


def values(conn):
    return conn.execute('SELECT shots_on_target_2h_home,shots_on_target_2h_away,goals_2h_home,goals_2h_away,shots_including_goals_2h_home,shots_including_goals_2h_away,shots_including_goals_2h_total FROM fonbet_live_statistics_75_normalized').fetchone()


def test_counts_only_second_half_goals_and_never_increments_raw_on_reimport(conn):
    row=add_fonbet(conn)
    row.update(goals_home=3,goals_away=1,ht_home=1,ht_away=1,shots_on_target_2h_home=4,shots_on_target_2h_away=2)
    # A newer reading wins; repeated import of exactly the same observation is idempotent.
    row['observed_at']='2026-11-10T10:00:03Z'
    row['observation_id']='00000000-0000-4000-8000-000000000777'
    data=dict(run_id=row['run_id'],started_at=row['requested_at'],finished_at=row['observed_at'],status='complete',summary={},observations=[row])
    for _ in range(2):conn.execute(build_sql(data))
    assert values(conn)==(4,2,2,0,6,2,8)
    assert conn.execute("SELECT statistics->'home'->'2h'->>'shots_on_goal' FROM live_match_statistics_75_combined").fetchone()==('6',)
    payload=conn.execute('SELECT payload FROM fonbet_live_observations WHERE observation_id=%s',(row['observation_id'],)).fetchone()[0]
    # Archive stores the source value, not the normalized six shots.
    assert payload['shots_on_target_2h_home']==4
    assert payload['ht_home']==1


@pytest.mark.parametrize('ht,raw,expected',[(None,4,None),(1,None,None),(1,0,2),(3,0,0)])
def test_unknown_half_and_unknown_shots_are_not_zero(conn,ht,raw,expected):
    add_fonbet(conn)
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET goals_home=3,ht_home=%s,shots_on_target_2h_home=%s,quality_status=%s',(ht,raw,'partial' if raw is None else 'both_teams'))
    assert values(conn)[4]==expected


def test_reversal_happens_after_correction_and_api_never_gets_goal_increment(conn):
    seed_api(conn);row=add_fonbet(conn);reconcile_event(conn,row)
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET goals_home=3,ht_home=1,shots_on_target_2h_home=4,home_away_swapped=true')
    assert conn.execute("SELECT statistics->'away'->'2h'->>'shots_on_goal' FROM live_match_statistics_75_combined").fetchone()==('6',)
    add_api_statistics(conn)
    assert conn.execute("SELECT source,statistics->'home'->'2h'->>'shots_on_goal' FROM live_match_statistics_75_combined").fetchone()==('api_football','2')


def test_dispatcher_uses_corrected_fonbet_threshold_and_unknown_suppresses_it(conn):
    fb(conn,h=5,a=0)
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET ht_home=NULL,ht_away=NULL')
    assert run(conn,dry_run=True).get('would_trigger',0)==0
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET ht_home=0,ht_away=0')
    assert run(conn,dry_run=True)['would_trigger']==1
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET ht_home=1')
    assert run(conn,dry_run=True).get('would_trigger',0)==0
