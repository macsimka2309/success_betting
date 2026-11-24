import os
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace
import json

import pytest
from src.jobs.signal_tracking import configure
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_results import run_results
from src.jobs.signal_goals import Timeline, api_timeline, load_fonbet_timeline
from tests.test_collect import conn  # noqa: F401
from tests.test_alert_dispatch import fb,api,run,link,NOW
from tests.test_signal_goals import fixture,goal
from tests.test_signal_reminders import fresh_fonbet

pytestmark=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='disposable DB required')
FIXTURES=Path(__file__).parent/'fixtures/fonbet'


def first_signal(conn):
    configure(conn,tracking=True,reminders=True,now=NOW)
    fb(conn)
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET elapsed=67')
    conn.execute('UPDATE live_alert_context SET elapsed=67')
    assert run(conn)['sent']==1
    api(conn);link(conn)


def test_first_signal_reminder_primary_and_day_review(conn):
    first_signal(conn)
    later=NOW+timedelta(minutes=4)
    fresh_fonbet(conn,at=later)
    assert dispatch_reminders(conn,lambda *a:999,now_fn=lambda:later)['reminder_sent']==1
    def loader(url, **kw):
        return api_timeline(fixture([goal(10),goal(70),goal(78),goal(90,12)],score=(4,0))),None,{}
    ended=NOW+timedelta(minutes=45)
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:ended)['preliminary']==1
    assert conn.execute("SELECT count(*) FROM live_signal_goal_relations WHERE relation='after'").fetchone()[0]==3
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:ended+timedelta(days=1))['reviewed']==1
    assert conn.execute("SELECT count(*) FROM live_alert_outbox WHERE state='sent'").fetchone()[0]==1
    assert conn.execute("SELECT count(*) FROM live_signal_reminders WHERE state='sent'").fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==4


def test_fonbet_partial_then_unavailable_keeps_relations(conn):
    first_signal(conn)
    html=(FIXTURES/'timeline-seven-goals.html').read_text()
    def loader(url,**kw):
        data={'html':html,'source_event_id':'123','clock_text':'90','period_text':'Матч окончен'}
        return load_fonbet_timeline(url,runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(data)),**kw)
    end=NOW+timedelta(minutes=40)
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:end)['preliminary']==1
    count=conn.execute('SELECT count(*) FROM live_signal_goal_relations').fetchone()[0]
    assert count==7
    def unavailable(url,**kw):
        return load_fonbet_timeline(url,runner=lambda *a,**k:SimpleNamespace(returncode=75),**kw)
    assert run_results(conn,fonbet_loader=unavailable,now_fn=lambda:end+timedelta(days=1))['deferred_internal']==1
    assert conn.execute('SELECT count(*) FROM live_signal_goal_relations').fetchone()[0]==count
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==7


def test_reversed_fonbet_sides_align_without_rewriting_identity(conn):
    first_signal(conn)
    def loader(url,**kw):
        return api_timeline(fixture([goal(82)])),None,{}
    end=NOW+timedelta(minutes=45)
    run_results(conn,fonbet_loader=loader,now_fn=lambda:end)
    gid=conn.execute("SELECT goal_id FROM live_signal_goals_selected WHERE credited_side='home'").fetchone()[0]
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET home_away_swapped=true')
    run_results(conn,fonbet_loader=loader,now_fn=lambda:end+timedelta(days=1))
    assert conn.execute("SELECT goal_id,credited_side,score_after_away FROM live_signal_goals_selected").fetchall()==[(gid,'away',1)]
    assert conn.execute('SELECT timeline_complete FROM live_signal_tracking').fetchone()[0] is True


@pytest.mark.parametrize('first', ['api_football', 'fonbet'])
def test_late_merge_preserves_first_source_and_queue(conn,first):
    configure(conn,tracking=True,reminders=True,now=NOW)
    initial, second=(api,fb) if first=='api_football' else (fb,api)
    initial(conn);run(conn)
    second(conn);run(conn)
    link(conn)
    assert conn.execute("SELECT first_source,goals_enabled FROM live_signal_tracking WHERE status<>'void'").fetchall()==[(first,first=='fonbet')]
    calls=[]
    def loader(url,**kw):
        calls.append(url)
        return Timeline(status='2H',elapsed=89,period='regulation_2h'),None,{}
    run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW+timedelta(minutes=20))
    assert len(calls)==int(first=='fonbet')


def test_merge_during_fonbet_read_drops_superseded_work(conn):
    configure(conn,tracking=True,reminders=False,now=NOW)
    api(conn);run(conn)
    fb(conn);run(conn)
    assert conn.execute("SELECT count(*) FROM live_signal_tracking").fetchone()==(2,)

    def loader(url,**kw):
        import psycopg
        with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as other:
            other.execute("SET statement_timeout='2s'")
            link(other)
        return api_timeline(fixture([goal(90,12)])),None,{}

    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW+timedelta(minutes=45))=={}
    assert conn.execute("SELECT count(*) FROM live_signal_goal_observations").fetchone()==(0,)
    assert conn.execute("SELECT first_source FROM live_signal_tracking WHERE status<>'void'").fetchone()==('api_football',)
