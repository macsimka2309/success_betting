"""Integration tests for source priority, matching audit and canonical identity."""
import copy
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/fonbet_live'))
from store import build_sql
from reconcile import decision_sql, choose_match, DATA_SQL
from test_store import ROW
from tests.test_collect import conn  # noqa: F401
from tests.test_live_statistics import receipts
from tests.test_live_alerts import live_item
from src.jobs.live_statistics import save_statistics

pytestmark=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='temporary database required')


def add_fonbet(conn, event_id='123'):
    row=copy.deepcopy(ROW)
    row.update(fonbet_event_id=event_id,event_url='https://fon.bet/live/football/1/'+event_id,
               home_name='Home U20',away_name='Away U20',competition_name='Youth League',
               kickoff_at='2026-11-10T08:30:00Z',goals_home=1,goals_away=0,ht_home=1,ht_away=0)
    row['observation_id']=row['observation_id'][:-12]+event_id.zfill(12)
    data=dict(run_id=row['run_id'],started_at=row['requested_at'],finished_at=row['observed_at'],status='complete',summary={},observations=[row])
    conn.execute(build_sql(data))
    return row


def seed_api(conn):
    conn.execute("INSERT INTO leagues(league_id,name) VALUES(3,'Youth League')")
    conn.execute("INSERT INTO teams(team_id,name) VALUES(1,'Home U20'),(2,'Away U20')")
    conn.execute("INSERT INTO fixtures(fixture_id,league_id,season,kickoff_at,match_date,status_short,home_team_id,away_team_id) VALUES(10,3,2026,'2026-11-10 08:30Z','2026-11-10','FT',1,2)")


def add_api_statistics(conn):
    receipt,run_id=receipts(conn)
    match=live_item(10,minute=74,home=1,away=0)
    match['teams']['home']['id']=1;match['teams']['away']['id']=2
    match['league']['id']=3
    body={'response':[{'team':{'id':team},'statistics_2h':[{'type':'Shots on Goal','value':shots}]} for team,shots in ((1,2),(2,3))]}
    save_statistics(conn,match,body,receipt,receipt,run_id)


def reconcile_event(conn,row):
    query = DATA_SQL[DATA_SQL.index('WITH pending'):DATA_SQL.rindex('COMMIT;')]
    data = conn.execute(query).fetchone()[0]
    event=next(e for e in data['events'] if e['fonbet_event_id']==row['fonbet_event_id'])
    decision=choose_match(event,data['fixtures'],data['team_aliases'],data['league_aliases'])
    conn.execute(decision_sql(event,decision))
    return event,decision


def test_late_api_maps_fonbet_and_has_priority(conn):
    row=add_fonbet(conn)
    assert conn.execute('SELECT canonical_match_id,source,fixture_id FROM live_match_statistics_75_combined').fetchall()==[('fonbet:123','fonbet',None)]
    seed_api(conn)
    event,decision=reconcile_event(conn,row)
    assert decision['mapping_status']=='confirmed'
    assert conn.execute('SELECT canonical_match_id,source,fixture_id FROM live_match_statistics_75_combined').fetchall()==[('api:10','fonbet',10)]
    add_api_statistics(conn)
    result=conn.execute("SELECT canonical_match_id,source,elapsed,statistics->'home'->'2h'->>'shots_on_goal',fonbet_event_id FROM live_match_statistics_75_combined").fetchall()
    assert result==[('api:10','api_football',74,'2','123')]
    conn.execute(decision_sql(event,decision))
    assert conn.execute('SELECT count(*) FROM fonbet_fixture_mapping_audit').fetchone()[0]==1


def test_multiple_fonbet_events_collapse_and_reversed_teams_align(conn):
    seed_api(conn)
    row=add_fonbet(conn)
    reconcile_event(conn,row)
    second=add_fonbet(conn,'124')
    conn.execute("UPDATE fonbet_live_match_statistics_75 SET home_name='Away U20',away_name='Home U20' WHERE fonbet_event_id='124'")
    reconcile_event(conn,second)
    result=conn.execute("SELECT canonical_match_id,home_name,away_name,statistics->'home'->'2h'->>'shots_on_goal',statistics->'away'->'2h'->>'shots_on_goal' FROM live_match_statistics_75_combined").fetchall()
    assert result==[('api:10','Home U20','Away U20','4','0')]


def test_stale_decision_does_not_change_new_observation(conn):
    seed_api(conn);row=add_fonbet(conn)
    data=conn.execute("SELECT row_to_json(f) FROM fonbet_live_match_statistics_75 f").fetchone()[0]
    decision=dict(fixture_id=10,mapping_status='confirmed',home_away_swapped=False,mapping_reason='test',mapping_candidates=[10])
    data['observation_id']='00000000-0000-4000-8000-000000000999'
    conn.execute(decision_sql(data,decision))
    assert conn.execute('SELECT mapping_status FROM fonbet_live_match_statistics_75').fetchone()[0]=='unmatched'


def test_api_live_match_not_in_main_catalog_can_be_matched(conn):
    from psycopg.types.json import Jsonb
    seed_api(conn);add_api_statistics(conn)
    # Live collection also sees leagues outside the main tracked fixture catalog.
    conn.execute('DELETE FROM fixtures WHERE fixture_id=10')
    conn.execute('DELETE FROM teams WHERE team_id IN (1,2)')
    context=live_item(10,minute=75,home=1,away=0)
    context['teams']['home']['name']='Home U20';context['teams']['away']['name']='Away U20'
    context['league']['name']='Youth League'
    conn.execute("UPDATE api_observations SET payload=%s WHERE observation_id IN (SELECT context_observation_id FROM live_match_statistics_75)", (Jsonb({'response':[context]}),))
    conn.execute("UPDATE live_match_statistics_75 SET observed_at='2026-11-10T10:00:02Z'")
    row=add_fonbet(conn)
    row['kickoff_at']=None
    conn.execute("UPDATE fonbet_live_match_statistics_75 SET kickoff_at=NULL")
    event,decision=reconcile_event(conn,row)
    assert decision['fixture_id']==10
    assert conn.execute('SELECT source,home_name,away_name FROM live_match_statistics_75_combined').fetchall()==[('api_football','Home U20','Away U20')]


def test_candidate_date_bounds_include_explicit_kickoff_in_another_day(conn):
    seed_api(conn)
    conn.execute("INSERT INTO fixtures(fixture_id,league_id,season,kickoff_at,match_date,status_short,home_team_id,away_team_id) VALUES(11,3,2026,'2026-11-07 23:55Z','2026-11-07','FT',1,2)")
    row=add_fonbet(conn)
    conn.execute("UPDATE fonbet_live_match_statistics_75 SET kickoff_at='2026-11-08 00:04Z'")
    event,decision=reconcile_event(conn,row)
    assert decision['fixture_id']==11


def test_candidate_date_bounds_empty_queue(conn):
    seed_api(conn)
    query=DATA_SQL[DATA_SQL.index('WITH pending'):DATA_SQL.rindex('COMMIT;')]
    data=conn.execute(query).fetchone()[0]
    assert data['events']==[] and data['fixtures']==[]
