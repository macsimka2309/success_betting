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


def signal(conn,sid,when=NOW,review=False):
    mid=conn.execute('INSERT INTO live_alert_matches DEFAULT VALUES RETURNING alert_match_id').fetchone()[0]
    conn.execute('''INSERT INTO live_signal_tracking
       (signal_id,alert_match_id,feature_version,first_message_id,first_sent_at,
        confirmed_at,delivery_started_at,time_precision,first_context)
       VALUES(%s,%s,1,%s,%s,%s,%s,'delivery_interval',%s)''',
       (sid,mid,1000+sid,when,when,when,Jsonb({'current_minute':67,'context_at':when.isoformat()})))
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
    for sid in range(1,32):
        mid=signal(conn,sid,NOW-timedelta(minutes=31-sid))
        conn.execute('INSERT INTO live_signal_reminders(signal_id,next_attempt_at) VALUES(%s,%s)',(sid,NOW))
        conn.execute("INSERT INTO live_alert_aliases VALUES('api_football',%s,%s)",(str(sid),mid))
    conn.execute('''INSERT INTO live_alert_context(source,source_event_id,observed_at,status,elapsed,present,
       poll_id,requested_at,completed_at,period) VALUES('api_football','31',%s,'2H',70,true,'next',%s,%s,'regulation_2h')''',
       (NOW+timedelta(seconds=2),NOW+timedelta(seconds=1),NOW+timedelta(seconds=2)))
    sent=[]
    for _ in range(2):dispatch_reminders(conn,lambda *a:sent.append(a) or 999,now_fn=lambda:NOW+timedelta(seconds=3))
    assert len(sent)==1,'First 30 waiting rows prevent the ready 31st reminder forever'


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
    run_results(conn,None,fonbet_loader=loader,now_fn=lambda:NOW+timedelta(days=1),browser_limit=1)
    assert len(calls)==1
    days=conn.execute('SELECT signal_id,review_day FROM live_signal_tracking ORDER BY signal_id').fetchall()
    assert days==[(1,1),(2,0)],f'Only one request happened, but counters became {days}'


def test_archived_card_reader_metadata_must_expose_finished_status():
    html=(Path('/Users/mesalikhov/Dev/success_betting/tests/fixtures/fonbet/timeline-seven-goals.html').read_text()+
          '<div>Матч завершён</div>')
    # The no-discovery branch in timeline.cjs always emits period_text=''.
    raw={'html':html,'clock_text':'90:00','period_text':''}
    timeline,_,_=load_fonbet_timeline('https://fon.bet/live/football/1/2',runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(raw)))
    assert end_state(timeline.status)=='ended',f'Finished-card branch is read as {timeline.status}, {timeline.reason}'
