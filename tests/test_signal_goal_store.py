import os
from datetime import timedelta

import pytest
from src.jobs.signal_goals import api_timeline,Timeline
from src.jobs.signal_goal_store import save_timeline,choose_timeline,event_relation
from tests.test_signal_goals import fixture,goal
from tests.test_signal_tracking_schema import insert_signal
from tests.test_collect import conn  # noqa: F401
from tests.test_alert_dispatch import NOW

DB=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='disposable DB required')

@DB
def test_repeat_same_minute_and_var_correction(conn):
    mid=insert_signal(conn)
    data=api_timeline(fixture([goal(80),goal(80)],score=(2,0)))
    for _ in range(2):save_timeline(conn,mid,'api_football','10',data,NOW,{'version':1})
    assert conn.execute('SELECT count(*) FROM live_signal_goal_observations').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==2
    partial=Timeline(reason='timeline_unavailable')
    save_timeline(conn,mid,'api_football','10',partial,NOW+timedelta(seconds=1),{})
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==2
    data=api_timeline(fixture([goal(80)]))
    save_timeline(conn,mid,'api_football','10',data,NOW+timedelta(seconds=2),{'var':'cancelled'})
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM live_signal_goals').fetchone()[0]>=2
    assert conn.execute('SELECT count(*) FROM live_signal_goal_observations').fetchone()[0]==3

@DB
def test_canonical_merge_does_not_duplicate_active_source_events(conn):
    mid=insert_signal(conn)
    other=conn.execute('INSERT INTO live_alert_matches DEFAULT VALUES RETURNING alert_match_id').fetchone()[0]
    data=api_timeline(fixture([goal()]))
    save_timeline(conn,other,'api_football','10',data,NOW,{})
    conn.execute('UPDATE live_alert_matches SET merged_into=%s WHERE alert_match_id=%s',(mid,other))
    save_timeline(conn,mid,'api_football','10',data,NOW+timedelta(seconds=1),{})
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==1


def test_conflict_retains_api_primary():
    api=api_timeline(fixture([goal(80)]));fb=api_timeline(fixture([goal(81)]))
    selected,conflict=choose_timeline([('fonbet','1',fb,None,{}),('api_football','10',api,None,{})])
    assert conflict and selected[0]=='api_football'

@pytest.mark.parametrize('minute,result',[(66,'before'),(67,'order_unknown'),(68,'order_unknown'),(70,'after'),(78,'after'),(90,'after')])
def test_original_boundary_not_reminder(minute,result):
    signal={'first_context':{'current_minute':67,'context_at':NOW.isoformat()},'confirmed_at':NOW}
    assert event_relation({'minute':minute,'period':'regulation_2h'},signal)==result


def test_old_snapshot_unknown_not_false_after():
    signal={'first_context':{'current_minute':67,'context_at':NOW.isoformat()},'confirmed_at':NOW+timedelta(minutes=4)}
    assert event_relation({'minute':70,'period':'regulation_2h'},signal)=='order_unknown'

@DB
def test_relation_persistence_is_separate_from_completeness(conn):
    from psycopg.types.json import Jsonb
    from src.jobs.alert_dispatch import rows
    from src.jobs.signal_goal_store import relate_goals
    mid=insert_signal(conn)
    conn.execute('UPDATE live_signal_tracking SET first_context=%s,confirmed_at=%s,delivery_started_at=%s WHERE signal_id=1',
                 (Jsonb({'current_minute':67,'context_at':NOW.isoformat()}),NOW,NOW))
    data=api_timeline(fixture([goal(67),goal(70)],score=(2,0)))
    save_timeline(conn,mid,'api_football','10',data,NOW,{})
    signal=rows(conn,'SELECT * FROM live_signal_tracking')[0]
    assert relate_goals(conn,signal,'api_football')==1
    assert conn.execute("SELECT relation FROM live_signal_goal_relations ORDER BY goal_id").fetchall()==[('order_unknown',),('after',)]
    assert data.complete

@DB
def test_public_goal_projection_does_not_sum_sources(conn):
    from src.jobs.alert_dispatch import rows
    from src.jobs.signal_goal_store import relate_goals
    mid=insert_signal(conn)
    data=api_timeline(fixture([goal()]))
    save_timeline(conn,mid,'api_football','10',data,NOW,{})
    save_timeline(conn,mid,'fonbet','123',data,NOW,{})
    relate_goals(conn,rows(conn,'SELECT * FROM live_signal_tracking')[0],'api_football')
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==2
    assert conn.execute('SELECT count(*),min(source) FROM live_signal_goals_selected').fetchone()==(1,'api_football')

@DB
def test_partial_retains_id_and_known_score_then_corrected_minute(conn):
    mid=insert_signal(conn)
    save_timeline(conn,mid,'api_football','10',api_timeline(fixture([goal(80)])),NOW,{})
    gid=conn.execute('SELECT goal_id FROM live_signal_goals').fetchone()[0]
    save_timeline(conn,mid,'api_football','10',api_timeline(fixture([goal(80)],score=(2,0))),NOW+timedelta(seconds=1),{})
    assert conn.execute('SELECT goal_id,score_after_home FROM live_signal_goals WHERE active').fetchall()==[(gid,1)]
    save_timeline(conn,mid,'api_football','10',api_timeline(fixture([goal(81)])),NOW+timedelta(seconds=2),{})
    assert conn.execute('SELECT goal_id,minute FROM live_signal_goals WHERE active').fetchall()==[(gid,81)]
    assert conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone()[0]==3

@DB
def test_ambiguous_partial_is_not_an_extra_goal(conn):
    mid=insert_signal(conn)
    save_timeline(conn,mid,'api_football','10',api_timeline(fixture([goal(80),goal(80)],score=(2,0))),NOW,{})
    partial=api_timeline(fixture([goal(80)],score=(3,0)))
    save_timeline(conn,mid,'api_football','10',partial,NOW+timedelta(seconds=1),{})
    assert partial.reason=='identity_ambiguous'
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==2
    assert conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone()[0]==1

@DB
def test_full_snapshot_collapses_duplicate_rows_from_merged_aliases(conn):
    mid=insert_signal(conn)
    other=conn.execute('INSERT INTO live_alert_matches DEFAULT VALUES RETURNING alert_match_id').fetchone()[0]
    data=api_timeline(fixture([goal()]))
    save_timeline(conn,mid,'fonbet','123',data,NOW,{})
    save_timeline(conn,other,'fonbet','124',data,NOW,{})
    conn.execute('UPDATE live_alert_matches SET merged_into=%s WHERE alert_match_id=%s',(mid,other))
    save_timeline(conn,mid,'fonbet','123',data,NOW+timedelta(seconds=1),{})
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==1

@DB
def test_correction_of_base_minute_does_not_carry_old_stoppage(conn):
    mid=insert_signal(conn)
    save_timeline(conn,mid,'api_football','10',api_timeline(fixture([goal(90,2)])),NOW,{})
    gid=conn.execute('SELECT goal_id FROM live_signal_goals').fetchone()[0]
    save_timeline(conn,mid,'api_football','10',api_timeline(fixture([goal(80)])),NOW+timedelta(seconds=1),{})
    assert conn.execute('SELECT goal_id,minute,added_minute FROM live_signal_goals WHERE active').fetchone()==(gid,80,None)

@DB
def test_late_inserted_goal_does_not_steal_later_goal_identity(conn):
    mid=insert_signal(conn)
    save_timeline(conn,mid,'api_football','10',api_timeline(fixture([goal(80)])),NOW,{})
    gid=conn.execute('SELECT goal_id FROM live_signal_goals').fetchone()[0]
    save_timeline(conn,mid,'api_football','10',api_timeline(fixture([goal(70),goal(80)],score=(2,0))),NOW+timedelta(seconds=1),{})
    assert conn.execute('SELECT minute,score_after_home FROM live_signal_goals WHERE goal_id=%s',(gid,)).fetchone()==(80,2)
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==2

@DB
def test_repeated_indistinguishable_partial_events_are_pending(conn):
    mid=insert_signal(conn)
    partial=api_timeline(fixture([goal(80),goal(80)],score=(3,0)))
    save_timeline(conn,mid,'api_football','10',partial,NOW,{})
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone()[0]==1

@DB
def test_cancelled_goal_requires_newer_complete_timeline_to_reactivate(conn):
    mid=insert_signal(conn)
    initial=api_timeline(fixture([goal(80)]))
    cancelled=api_timeline(fixture([],score=(0,0)))
    partial=api_timeline(fixture([goal(80)],score=(2,0)))
    save_timeline(conn,mid,'fonbet','123',initial,NOW,{})
    gid=conn.execute('SELECT goal_id FROM live_signal_goals').fetchone()[0]
    save_timeline(conn,mid,'fonbet','123',cancelled,NOW+timedelta(seconds=2),{})
    save_timeline(conn,mid,'fonbet','123',partial,NOW+timedelta(seconds=3),{})
    save_timeline(conn,mid,'fonbet','123',initial,NOW+timedelta(seconds=1),{})
    assert conn.execute('SELECT goal_id,active FROM live_signal_goals').fetchall()==[(gid,False)]
    save_timeline(conn,mid,'fonbet','123',api_timeline(fixture([goal(80)])),NOW+timedelta(seconds=4),{})
    assert conn.execute('SELECT goal_id,active FROM live_signal_goals').fetchall()==[(gid,True)]
    assert conn.execute('SELECT count(*) FROM live_signal_goal_observations').fetchone()==(5,)


@DB
def test_equal_observation_time_uses_archive_order_and_keeps_var_history(conn):
    mid = insert_signal(conn)

    def observed(goals, score, at=NOW, **payload):
        with conn.transaction():
            return save_timeline(conn, mid, 'api_football', '10',
                                 api_timeline(fixture(goals, score=score)), at, payload)

    def goal_state():
        return conn.execute("""
            SELECT goal_id,observation_id,active,revision FROM live_signal_goals
        """).fetchone()

    first = observed([goal(80)], (1, 0))
    gid, *_ = goal_state()
    assert goal_state() == (gid, first, True, 1)
    cancelled = observed([], (0, 0), var='cancelled')
    assert cancelled > first
    assert goal_state() == (gid, cancelled, False, 2)
    assert observed([goal(80)], (1, 0)) == first
    assert observed([], (0, 0), var='cancelled') == cancelled
    observed([goal(80)], (1, 0), NOW - timedelta(seconds=1), delayed=True)
    assert goal_state() == (gid, cancelled, False, 2)
    assert conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone() == (2,)
    restored = observed([goal(80)], (1, 0), NOW + timedelta(seconds=1), var='restored')
    assert goal_state() == (gid, restored, True, 3)
    assert conn.execute("""
        SELECT observation_id,payload->>'active' FROM live_signal_goal_versions ORDER BY observation_id
    """).fetchall() == [(first, 'true'), (cancelled, 'false'), (restored, 'true')]


@DB
def test_own_goal_without_credited_side_keeps_id_or_remains_pending(conn):
    mid=insert_signal(conn)
    own=goal(80,detail='Own Goal')
    complete=api_timeline(fixture([own],score=(0,1)))
    partial=api_timeline(fixture([own],score=(0,3)))
    save_timeline(conn,mid,'api_football','10',complete,NOW,{})
    gid=conn.execute('SELECT goal_id FROM live_signal_goals').fetchone()[0]
    save_timeline(conn,mid,'api_football','10',partial,NOW+timedelta(seconds=1),{})
    assert conn.execute('SELECT goal_id,credited_side FROM live_signal_goals').fetchall()==[(gid,'away')]
    partial.goals[0]['source_team_id']=None
    save_timeline(conn,mid,'api_football','10',partial,NOW+timedelta(seconds=2),{})
    assert conn.execute('SELECT count(*) FROM live_signal_goals').fetchone()==(1,)
    assert conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone()==(1,)


@DB
@pytest.mark.parametrize('chained_merge', [False, True])
def test_repeat_after_alias_merge_reuses_source_observation(conn, chained_merge):
    canonical, = conn.execute("SELECT live_alert_identity('api_football','1')").fetchone()
    intermediate, = conn.execute("SELECT live_alert_identity('api_football','2')").fetchone()
    original_match, = conn.execute("SELECT live_alert_identity('fonbet','123')").fetchone()

    def observe(match_id, *, event_id='123', sample=1):
        timeline = Timeline(goals=[dict(
            credited_side='home', minute=84, period='regulation_2h', detail='Гол',
            score_after_home=1, score_after_away=0,
        )], status='2H')
        with conn.transaction():
            return save_timeline(conn, match_id, 'fonbet', event_id, timeline, NOW,
                                 {'structure_valid': True, 'sample': sample})

    def goal_state():
        return conn.execute("""
            SELECT goal_id,observation_id,active,revision FROM live_signal_goals
        """).fetchone()

    first = observe(original_match)
    goal_id, *_ = goal_state()
    assert goal_state() == (goal_id, first, True, 1)
    if chained_merge:
        assert conn.execute("SELECT live_alert_link('123',2)").fetchone() == (intermediate,)
    assert conn.execute("SELECT live_alert_link('123',1)").fetchone() == (canonical,)

    assert observe(canonical) == first
    assert observe(original_match) == first
    assert goal_state() == (goal_id, first, True, 1)
    assert conn.execute('SELECT count(*) FROM live_signal_goal_observations').fetchone() == (1,)
    assert conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone() == (1,)

    changed = observe(canonical, sample=2)
    assert changed > first
    assert goal_state() == (goal_id, changed, True, 2)
    assert observe(canonical) == first
    assert goal_state() == (goal_id, changed, True, 2)

    assert conn.execute("SELECT live_alert_link('124',1)").fetchone() == (canonical,)
    distinct_source_event = observe(canonical, event_id='124', sample=2)
    assert distinct_source_event > changed
    assert goal_state() == (goal_id, distinct_source_event, True, 3)
    assert conn.execute("""
        SELECT source_event_id,observed_at FROM live_signal_goal_observations ORDER BY observation_id
    """).fetchall() == [('123', NOW), ('123', NOW), ('124', NOW)]
    assert conn.execute("""
        SELECT observation_id FROM live_signal_goal_versions ORDER BY observation_id
    """).fetchall() == [(first,), (changed,), (distinct_source_event,)]


@DB
def test_repeat_after_merge_with_legacy_copies_keeps_earliest_archive_id(conn):
    root, = conn.execute("SELECT live_alert_identity('api_football','1')").fetchone()
    alias, = conn.execute("SELECT live_alert_identity('fonbet','123')").fetchone()

    def observe(match_id):
        timeline = Timeline(goals=[dict(
            credited_side='home', minute=84, period='regulation_2h', detail='Гол',
            score_after_home=1, score_after_away=0,
        )], status='2H')
        with conn.transaction():
            return save_timeline(conn, match_id, 'fonbet', '123', timeline, NOW, {'structure_valid': True})

    # Prior to alias matching, the same source receipt could be stored under
    # both identities. Keep their existing audit rows on replay.
    old, duplicate = observe(alias), observe(root)
    assert duplicate > old
    assert conn.execute("SELECT live_alert_link('123',1)").fetchone() == (root,)
    previous = conn.execute("""
        SELECT goal_id,observation_id,active,revision FROM live_signal_goals ORDER BY goal_id
    """).fetchall()
    assert observe(root) == old
    assert conn.execute("""
        SELECT goal_id,observation_id,active,revision FROM live_signal_goals ORDER BY goal_id
    """).fetchall() == previous
    assert conn.execute('SELECT count(*) FROM live_signal_goal_observations').fetchone() == (2,)
    assert conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone() == (2,)
