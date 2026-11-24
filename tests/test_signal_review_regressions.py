import os
import pytest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import json

from psycopg.types.json import Jsonb
from tests.test_collect import conn  # noqa: F401
from tests.test_alert_dispatch import NOW
from tests.test_signal_goals import fixture,goal
from tests.test_live_source_matching import add_fonbet
from src.jobs.signal_tracking import configure
from src.jobs.signal_goals import api_timeline,Timeline,load_fonbet_timeline
from src.jobs.signal_goal_store import save_timeline,relate_goals
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_results import run_results,end_state
from src.jobs.alert_dispatch import rows


def signal(conn,sid,when=NOW,review=False,source='fonbet',eid=None):
    eid=eid or str(100+sid)
    mid=conn.execute('INSERT INTO live_alert_matches DEFAULT VALUES RETURNING alert_match_id').fetchone()[0]
    conn.execute('''INSERT INTO live_signal_tracking
       (signal_id,alert_match_id,feature_version,first_message_id,first_sent_at,
        confirmed_at,delivery_started_at,time_precision,first_context,first_source,first_source_event_id,goals_enabled)
       VALUES(%s,%s,2,%s,%s,%s,%s,'delivery_interval',%s,%s,%s,%s)''',
       (sid,mid,1000+sid,when,when,when,Jsonb({'current_minute':67,'context_at':when.isoformat()}),source,eid,source=='fonbet'))
    if review:
        conn.execute("UPDATE live_signal_tracking SET status='preliminary',regulation_end_confirmed_at=%s,review_due_at=%s,next_attempt_at=%s WHERE signal_id=%s",
                     (NOW,NOW+timedelta(days=1),NOW+timedelta(days=1),sid))
    return mid


def test_partial_nonempty_response_must_not_duplicate_known_goal(conn):
    mid=signal(conn,1)
    complete=api_timeline(fixture([goal(80)],score=(1,0)))
    partial=api_timeline(fixture([goal(80)],score=(2,0)))
    assert complete.complete and not partial.complete
    with conn.transaction():
        save_timeline(conn,mid,'api_football','10',complete,NOW,{'version':1})
        save_timeline(conn,mid,'api_football','10',partial,NOW+timedelta(seconds=1),{'version':2})
        relate_goals(conn,rows(conn,'SELECT * FROM live_signal_tracking')[0],'api_football')
    found=conn.execute("SELECT count(*) FROM live_signal_goals_selected WHERE relation='after'").fetchone()[0]
    assert found==1,f'One known goal is counted {found} times after a partial response'


def test_waiting_reminders_must_not_starve_ready_31st_signal(conn):
    configure(conn,tracking=True,reminders=True,now=NOW-timedelta(hours=1))
    from tests.test_alert_dispatch import fb
    from tests.test_signal_reminders import fresh_fonbet
    fb(conn,eid='131')
    for sid in range(1,32):
        mid=signal(conn,sid,NOW-timedelta(seconds=32-sid))
        conn.execute('INSERT INTO live_signal_reminders(signal_id,next_attempt_at) VALUES(%s,%s)',(sid,NOW))
        conn.execute("INSERT INTO live_alert_aliases VALUES('fonbet',%s,%s)",(str(100+sid),mid))
    fresh_fonbet(conn,eid='131')
    sent=[]
    for _ in range(2):dispatch_reminders(conn,lambda *a:sent.append(a) or 999,now_fn=lambda:NOW+timedelta(seconds=3))
    assert len(sent)==1,'First 30 waiting rows prevent the ready 31st reminder forever'


pytestmark=pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),reason="disposable database required")

def test_internal_browser_budget_must_not_consume_daily_check(conn):
    configure(conn,tracking=True,reminders=False,now=NOW)
    for sid in (1,2):
        mid=signal(conn,sid,review=True)
        eid=str(100+sid)
        add_fonbet(conn,eid)
        conn.execute("INSERT INTO live_alert_aliases VALUES('fonbet',%s,%s)",(eid,mid))
    calls=[]
    def loader(url,**kw):
        calls.append(url)
        return Timeline(status='FT',reason='timeline_unavailable'),None,{}
    run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW+timedelta(days=1),browser_limit=1)
    assert len(calls)==1
    days=conn.execute('SELECT signal_id,review_day FROM live_signal_tracking ORDER BY signal_id').fetchall()
    assert days==[(1,1),(2,0)],f'Only one request happened, but counters became {days}'



def test_archived_card_reader_metadata_must_expose_finished_status():
    # Replace the original synthetic free-text banner with the verified finished
    # result DOM: selected Completed filter plus the matching event identity.
    html=(Path(__file__).parent/'fixtures/fonbet/completed-result.html').read_text()
    raw={'completion_html':html,'clock_text':'90:00','period_text':''}
    timeline,_,_=load_fonbet_timeline('https://fon.bet/live/football/11978/69626793',runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(raw)))
    assert end_state(timeline.status)=='ended'


@pytest.mark.parametrize('api_run_status', ['success', 'quota_exceeded'])
def test_fonbet_still_runs_after_empty_api_or_exhausted_quota(conn, monkeypatch, api_run_status):
    from src.api.client import ApiClient
    from tests.test_signal_lifecycle import first_signal

    first_signal(conn)
    run_id=conn.execute("""INSERT INTO collection_runs(job_name,status)
        VALUES('live_alerts',%s) RETURNING run_id""",(api_run_status,)).fetchone()[0]
    if api_run_status=='success':
        conn.execute("""INSERT INTO api_observations(run_id,endpoint,params,requested_at,observed_at,payload)
            VALUES(%s,'/fixtures','{"live":"all"}',%s,%s,'{"response":[]}')""",(run_id,NOW,NOW))
    else:
        # This case also exercises the Fonbet-only route from the production defect.
        conn.execute("DELETE FROM live_alert_aliases WHERE source='api_football'")
    monkeypatch.setattr(ApiClient,'get',lambda *a,**k:pytest.fail('goal worker queried API'))
    seen=[]
    def loader(url,**kw):
        seen.append(url)
        return Timeline(status='REG_END',reason='timeline_unavailable'),None,{}
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW+timedelta(minutes=45))=={'preliminary':1}
    assert len(seen)==1
