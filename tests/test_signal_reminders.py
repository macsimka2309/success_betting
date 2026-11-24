import os
from datetime import timedelta

import pytest
from src.jobs.signal_reminders import decision,dispatch_reminders
from src.jobs.signal_tracking import configure
from src.jobs.alert_dispatch import DeliveryFailure
from tests.test_collect import conn  # noqa: F401
from tests.test_alert_dispatch import fb,run,NOW

DB=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='disposable DB required')

def context(**kw):
    base=dict(source='fonbet',source_event_id='123',poll_id='new',requested_at=NOW+timedelta(seconds=1),
              completed_at=NOW+timedelta(seconds=2),status='2H',period='regulation_2h',present=True,elapsed=70)
    return dict(base, **kw)


def decide(change=None, stat=None, at=3):
    current=context(**(change or {}))
    snapshot=dict(observed_at=NOW+timedelta(seconds=2),context_at=current['completed_at'],
                  statistics_context_at=NOW+timedelta(seconds=1),statistics_requested_at=NOW+timedelta(seconds=1),
                  present=current['present'],status=current['status'],current_minute=current['elapsed'],
                  elapsed=70,league='Premier League',home_shots=6,away_shots=0,statistics_poll_id='new')
    snapshot.update(stat or {})
    job=dict(confirmed_at=NOW,first_source='fonbet',first_source_event_id='123')
    return decision(job,[current],snapshot,NOW+timedelta(seconds=at))[0]


@pytest.mark.parametrize('change,stat,state',[
 ({},{},'ready'), ({'elapsed':75},{},'ready'),
 ({'elapsed':80},{},'outside_window'), ({'elapsed':90,'added_minute':12},{},'outside_window'),
 ({'requested_at':NOW-timedelta(seconds=1)},{},'waiting'),
 ({'source':'api_football'},{},'waiting'),
 ({'poll_id':None},{},'waiting'), ({'present':False,'status':'absent'},{},'waiting'),
 ({'status':'FT'},{},'regulation_end_or_void'), ({'status':'ET'},{},'regulation_end_or_void'),
 ({'period':'unknown','status':'unknown'},{},'waiting'),
 ({},{'statistics_requested_at':NOW},'waiting'),
 ({},{'statistics_context_at':NOW},'waiting'),
 ({},{'home_shots':5,'away_shots':None},'waiting'),
 ({},{'home_shots':5,'away_shots':0},'trigger_cleared'),
])
def test_decision(change,stat,state):
    assert decide(change,stat)==state


def test_stale_and_expired():
    assert decide(at=180)=='waiting'
    assert decide(at=1200)=='window_expired'


def fresh_fonbet(conn, at=NOW+timedelta(seconds=3), minute=71, eid='123'):
    conn.execute("""UPDATE fonbet_live_match_statistics_75 SET elapsed=%s,
      requested_at=%s,observed_at=%s,context_observed_at=%s WHERE fonbet_event_id=%s""",
      (minute,at-timedelta(seconds=2),at,at-timedelta(seconds=1),eid))
    conn.execute("""UPDATE live_alert_context SET elapsed=%s,period='regulation_2h',poll_id=(SELECT run_id::text FROM fonbet_live_match_statistics_75 WHERE fonbet_event_id=%s),
      requested_at=%s,completed_at=%s,observed_at=%s WHERE source='fonbet' AND source_event_id=%s""",
      (minute,eid,at-timedelta(seconds=2),at-timedelta(seconds=1),at-timedelta(seconds=1),eid))


def seed(conn):
    configure(conn,tracking=True,reminders=True,now=NOW)
    fb(conn);run(conn)
    fresh_fonbet(conn)

@DB
def test_one_reminder_requires_statistics(conn):
    seed(conn)
    sent=[]
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET requested_at=%s',(NOW,))
    dispatch_reminders(conn,lambda *a:pytest.fail('old request'),now_fn=lambda:NOW+timedelta(seconds=3))
    fresh_fonbet(conn)
    assert dispatch_reminders(conn,lambda t,m:sent.append(t) or 99,now_fn=lambda:NOW+timedelta(seconds=3))['reminder_sent']==1
    assert 'Напоминание' in sent[0] and 'первое сообщение' in sent[0]
    dispatch_reminders(conn,lambda *a:pytest.fail('third message'),now_fn=lambda:NOW+timedelta(seconds=4))
    assert conn.execute('SELECT count(*) FROM live_alert_outbox').fetchone()[0]==1

@DB
def test_definitive_negative_closes_without_stopping_fonbet(conn):
    seed(conn)
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET shots_on_target_2h_home=0')
    assert dispatch_reminders(conn,lambda *a:pytest.fail('trigger false'),now_fn=lambda:NOW+timedelta(seconds=3))['reminder_cancelled']==1
    assert conn.execute('SELECT state,reason FROM live_signal_reminders').fetchone()==('cancelled','trigger_cleared')
    assert conn.execute('SELECT goals_enabled FROM live_signal_tracking').fetchone()==(True,)

@DB
def test_failure_waits_then_ends(conn):
    seed(conn)
    conn.execute("UPDATE live_alert_context SET poll_id=NULL")
    assert dispatch_reminders(conn,lambda *a:pytest.fail('missing context'),now_fn=lambda:NOW+timedelta(seconds=3))=={}
    conn.execute("UPDATE live_alert_context SET poll_id='new',status='FT'")
    assert dispatch_reminders(conn,lambda *a:pytest.fail('ended'),now_fn=lambda:NOW+timedelta(seconds=3))['reminder_cancelled']==1

@DB
def test_unknown_delivery_and_restart(conn):
    seed(conn)
    def unknown(*a):raise DeliveryFailure()
    assert dispatch_reminders(conn,unknown,now_fn=lambda:NOW+timedelta(seconds=3))['reminder_uncertain']==1
    dispatch_reminders(conn,lambda *a:pytest.fail('blind replay'),now_fn=lambda:NOW+timedelta(seconds=4))

@DB
def test_crashed_sender(conn):
    seed(conn)
    conn.execute("UPDATE live_signal_reminders SET state='sending',lease_until=%s",(NOW,))
    dispatch_reminders(conn,lambda *a:pytest.fail('blind replay'),now_fn=lambda:NOW+timedelta(seconds=3))
    assert conn.execute('SELECT state FROM live_signal_reminders').fetchone()[0]=='uncertain'

@DB
def test_definite_failure_can_retry_but_never_twice_after_success(conn):
    seed(conn)
    def rejected(*a):raise DeliveryFailure(True)
    assert dispatch_reminders(conn,rejected,now_fn=lambda:NOW+timedelta(seconds=3))['reminder_retry']==1
    assert dispatch_reminders(conn,lambda *a:123,now_fn=lambda:NOW+timedelta(seconds=20))['reminder_sent']==1
    assert dispatch_reminders(conn,lambda *a:pytest.fail('third message'),now_fn=lambda:NOW+timedelta(seconds=21))=={}

@DB
def test_other_worker_lock(conn):
    import psycopg
    seed(conn)
    with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as other:
        other.execute('SELECT pg_advisory_lock(27001)')
        assert dispatch_reminders(conn,lambda *a:pytest.fail('concurrent send'))=={'reminder_busy':1}
        other.execute('SELECT pg_advisory_unlock(27001)')

@DB
def test_late_merge_preserves_first_boundary_and_only_one_reminder(conn):
    from tests.test_alert_dispatch import api,link
    configure(conn,tracking=True,reminders=True,now=NOW)
    fb(conn);run(conn)
    api(conn)
    run(conn)  # Without a confirmed mapping these identities are distinct.
    assert conn.execute('SELECT count(*) FROM live_signal_tracking').fetchone()[0]==2
    link(conn)
    assert conn.execute("SELECT count(*) FROM live_signal_reminders WHERE state='waiting'").fetchone()[0]==1
    assert conn.execute('SELECT count(DISTINCT alert_match_id) FROM live_signal_tracking').fetchone()[0]==1
    assert conn.execute("SELECT count(*) FROM live_signal_tracking WHERE status='void'").fetchone()[0]==1

@DB
def test_merge_after_reminder_never_replays(conn):
    from tests.test_alert_dispatch import api,link
    seed(conn)
    dispatch_reminders(conn,lambda *a:123,now_fn=lambda:NOW+timedelta(seconds=3))
    api(conn);run(conn)
    link(conn)
    assert conn.execute("SELECT count(*) FROM live_signal_reminders WHERE state IN ('waiting','retry')").fetchone()[0]==0


def test_real_sender_encodes_reply_with_safe_missing_original_fallback(monkeypatch):
    import json
    from types import SimpleNamespace
    from src.bot.telegram_api import TelegramApi
    from src.jobs.alert_dispatch import telegram_sender
    captured=[]
    monkeypatch.setattr(TelegramApi,'request',lambda self,method,params,timeout:captured.append(params) or (['curl'],'test config'))
    sender=telegram_sender('test',1,runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout='{"ok":true,"result":{"message_id":999}}'))
    assert sender.reply('reminder',17)==999
    assert json.loads(captured[0]['reply_parameters'])=={'message_id':17,'allow_sending_without_reply':True}

@DB
def test_reminder_uses_reply_transport_when_available(conn):
    seed(conn);captured=[]
    def sender(*a):pytest.fail('reply transport must be used')
    sender.reply=lambda text,mid:captured.append(mid) or 99
    dispatch_reminders(conn,sender,now_fn=lambda:NOW+timedelta(seconds=3))
    assert captured==[12345]

@DB
def test_no_send_lease_when_shared_service_budget_exhausted(conn):
    seed(conn)
    result=dispatch_reminders(conn,lambda *a:pytest.fail('no time for send'),now_fn=lambda:NOW+timedelta(seconds=3),clock=lambda:80,deadline=85)
    assert result['reminder_budget_deferred']==1
    assert conn.execute('SELECT state,attempts FROM live_signal_reminders').fetchone()==('waiting',0)

@DB
def test_initial_dispatch_respects_same_deadline(conn):
    from src.jobs.alert_dispatch import dispatch
    from tests.test_alert_dispatch import fb
    fb(conn)
    assert dispatch(conn,lambda *a:pytest.fail('no time for initial send'),now_fn=lambda:NOW,clock=lambda:80,deadline=85)['time_budget']==1
    assert conn.execute('SELECT state,attempts FROM live_alert_outbox').fetchone()==('pending',0)


@DB
def test_api_reminder_uses_its_own_new_shots_then_ends(conn):
    from src.jobs.signal_results import run_results
    configure(conn,tracking=True,reminders=True,now=NOW)
    archive_api_poll(conn, NOW, 70, 6)
    assert run(conn)['sent'] == 1
    fb(conn);fresh_fonbet(conn)
    at=NOW+timedelta(seconds=5)
    assert dispatch_reminders(conn,lambda *a:pytest.fail('old API data'),now_fn=lambda:at)=={}
    archive_api_poll(conn, at, 73, 6)
    assert dispatch_reminders(conn,lambda *a:999,now_fn=lambda:at)=={'reminder_sent':1}
    assert run_results(conn,now_fn=lambda:at,fonbet_loader=lambda *a,**k:pytest.fail('API result polling'))=={}


def archive_fonbet_poll(conn, at, minute, home, away=0, *, context_start=None, error=False):
    from uuid import uuid4
    from tests.test_live_source_matching import ROW
    from tools.fonbet_live.store import build_sql

    requested = at-timedelta(seconds=2)
    observed_context = at-timedelta(seconds=1)
    row = dict(ROW, elapsed=minute, clock_text=f'{minute}:00',
               goals_home=0, goals_away=0, ht_home=0, ht_away=0,
               shots_on_target_2h_home=home, shots_on_target_2h_away=away,
               quality_status='both_teams' if away is not None else 'partial',
               run_id=str(uuid4()), observation_id=str(uuid4()),
               discovered_at=requested.isoformat(), requested_at=requested.isoformat(),
               context_observed_at=observed_context.isoformat(), observed_at=at.isoformat())
    if error:
        row.update(status='error',reason='timeout')
    context = dict(fonbet_event_id='123', clock_text=row['clock_text'],
                   period_text='2-й тайм', observed_at=observed_context.isoformat())
    export = dict(run_id=row['run_id'], started_at=(context_start or requested).isoformat(),
                  finished_at=at.isoformat(), status='complete', summary={}, observations=[row],
                  contexts=[context])
    conn.execute(build_sql(export))
    return row['observation_id']


def archive_api_poll(conn, at, minute, home, away=0, *, context_start=None, error=False, team_ids=(10, 20)):
    from psycopg.types.json import Jsonb
    from src.api.client import ObservationReceipt
    from src.jobs.live_context import record_api_context
    from src.jobs.live_statistics import save_statistics
    from tests.test_live_alerts import live_item
    from tests.test_live_statistics import body

    run_id = conn.execute("""INSERT INTO collection_runs(job_name,status)
        VALUES('live_alerts','success') RETURNING run_id""").fetchone()[0]

    def observe(endpoint, params, requested, observed, payload):
        oid = conn.execute("""INSERT INTO api_observations(run_id,endpoint,params,
            requested_at,observed_at,payload) VALUES(%s,%s,%s,%s,%s,%s) RETURNING observation_id""",
            (run_id,endpoint,Jsonb(params),requested,observed,Jsonb(payload))).fetchone()[0]
        return ObservationReceipt(requested.timestamp(),observed.timestamp(),oid)

    match = live_item(1, minute=minute, home=0, away=0)
    for side, team_id in zip(('home', 'away'), team_ids):
        match['teams'][side]['id'] = team_id
    context = observe('/fixtures',{'live':'all'},context_start or at-timedelta(seconds=3),
                      at-timedelta(seconds=2),{'response':[match]})
    record_api_context(conn,[match],context)
    payload = {'errors':['timeout'],'response':[]} if error else body(home,away)
    for stat in payload['response']:
        stat['team']['id'] = team_ids[0 if stat['team']['id'] == 10 else 1]
    stats = observe('/fixtures/statistics',{'fixture':1,'half':'true'},at-timedelta(seconds=1),at,payload)
    if not error:
        save_statistics(conn,match,payload,stats,context,run_id)
    return str(stats.observation_id)


@DB
@pytest.mark.parametrize('publish', [archive_fonbet_poll,archive_api_poll])
def test_first_negative_poll_stays_closed_after_later_positive(conn,publish):
    configure(conn,tracking=True,reminders=True,now=NOW)
    publish(conn,NOW,67,6)
    assert run(conn)['sent']==1
    first = publish(conn,NOW+timedelta(minutes=5),70,5)
    at = NOW+timedelta(minutes=10)
    publish(conn,at,75,6)
    assert dispatch_reminders(conn,lambda *a:pytest.fail('first poll was negative'),
                              now_fn=lambda:at)=={'reminder_cancelled':1}
    observed = conn.execute('SELECT state,reason,first_decision FROM live_signal_reminders').fetchone()
    assert observed[:2]==('cancelled','trigger_cleared')
    assert observed[2]['observation_id']==first and observed[2]['triggered'] is False
    assert dispatch_reminders(conn,lambda *a:pytest.fail('closed after restart'),now_fn=lambda:at)=={}


@DB
@pytest.mark.parametrize('publish', [archive_fonbet_poll,archive_api_poll])
def test_archive_poll_below_retained_minute_still_controls_repeat(conn,publish):
    configure(conn,tracking=True,reminders=True,now=NOW)
    publish(conn,NOW,74,6)
    assert run(conn)['sent']==1
    at = NOW+timedelta(minutes=1)
    oid = publish(conn,at,73,6)
    assert dispatch_reminders(conn,lambda *a:999,now_fn=lambda:at)=={'reminder_sent':1}
    evidence = conn.execute('SELECT first_decision FROM live_signal_reminders').fetchone()[0]
    assert evidence['minute']==73 and evidence['observation_id']==oid
    table = 'fonbet_live_match_statistics_75' if publish is archive_fonbet_poll else 'live_match_statistics_75'
    assert conn.execute(f'SELECT elapsed FROM {table}').fetchall()==[(74,)]
    assert dispatch_reminders(conn,lambda *a:pytest.fail('duplicate reminder'),now_fn=lambda:at)=={}


@DB
@pytest.mark.parametrize('publish', [archive_fonbet_poll,archive_api_poll])
def test_invalid_early_poll_skipped_and_stale_positive_needs_confirmation(conn,publish):
    configure(conn,tracking=True,reminders=True,now=NOW)
    publish(conn,NOW,65,6)
    assert run(conn)['sent']==1
    publish(conn,NOW+timedelta(seconds=10),66,5,context_start=NOW-timedelta(seconds=1))
    publish(conn,NOW+timedelta(seconds=35),66,5,error=True)
    publish(conn,NOW+timedelta(minutes=1),67,5,None)
    first = publish(conn,NOW+timedelta(minutes=2),68,6)
    at = NOW+timedelta(minutes=5)
    assert dispatch_reminders(conn,lambda *a:pytest.fail('stale positive'),now_fn=lambda:at)=={}
    assert conn.execute('SELECT first_decision FROM live_signal_reminders').fetchone()[0]['observation_id']==first
    publish(conn,at,72,6)
    assert dispatch_reminders(conn,lambda *a:999,now_fn=lambda:at)=={'reminder_sent':1}
    assert conn.execute('SELECT first_decision FROM live_signal_reminders').fetchone()[0]['observation_id']==first


@DB
def test_cutover_retains_first_api_poll_when_archived_pairs_are_unique(conn):
    from src.db import migrate
    from tests.test_signal_review_regressions import signal

    configure(conn,tracking=True,reminders=True,now=NOW)
    archive_api_poll(conn,NOW,67,6)
    assert run(conn)['sent']==1
    first = archive_api_poll(conn,NOW+timedelta(minutes=5),70,5)
    latest = NOW+timedelta(minutes=10)
    archive_api_poll(conn,latest,75,6)
    signal_id = conn.execute('SELECT signal_id FROM live_signal_reminders').fetchone()[0]

    migration = next(m for m in migrate.discover() if m.version=='036')
    migrate.revert(conn,migration)
    # These delivery states already exist before migration 036.
    for sid,state in ((101,'sent'),(102,'uncertain'),(103,'cancelled')):
        signal(conn,sid,source='api_football',eid=str(sid))
        conn.execute("""INSERT INTO live_signal_reminders(signal_id,state,telegram_message_id,sent_at)
            VALUES(%s,%s,%s,%s)""",(sid,state,999 if state=='sent' else None,NOW if state=='sent' else None))
    migrate.apply(conn,migration)
    assert conn.execute('SELECT count(*) FROM live_api_reminder_observations').fetchone()==(0,)
    for _ in range(2):
        dispatch_reminders(conn,lambda *a:pytest.fail('earlier archived poll was negative'),now_fn=lambda:latest)
    state,reason,evidence = conn.execute("""
        SELECT state,reason,first_decision FROM live_signal_reminders WHERE signal_id=%s
        """,(signal_id,)).fetchone()
    assert (state,reason,evidence['minute'],evidence['observation_id'])==('cancelled','trigger_cleared',70,first)
    assert conn.execute("""SELECT state,first_decision FROM live_signal_reminders
        WHERE signal_id IN (101,102,103) ORDER BY signal_id""").fetchall()==[
        ('sent',None),('uncertain',None),('cancelled',None)]


def duplicate_archived_context(conn, context_id):
    conn.execute("""INSERT INTO api_observations(run_id,endpoint,params,requested_at,observed_at,payload)
        SELECT run_id,endpoint,params,requested_at-interval '100 milliseconds',
            observed_at-interval '100 milliseconds',payload
        FROM api_observations WHERE observation_id=%s""",(context_id,))


@DB
@pytest.mark.parametrize('variant',[
    'unique','ambiguous','too_old','different_fixture','different_run',
    'non_live_run','outside_window','different_team','started_before_signal',
])
def test_restore_api_archive_only_when_context_is_proven(conn,variant):
    configure(conn,tracking=True,reminders=True,now=NOW)
    archive_api_poll(conn,NOW,74,6)
    assert run(conn)['sent']==1
    at = NOW+timedelta(minutes=1)
    observation_id = archive_api_poll(conn,at,73,6)
    context_id = conn.execute("""DELETE FROM live_api_reminder_observations WHERE observation_id=%s
        RETURNING context_observation_id""",(int(observation_id),)).fetchone()[0]
    assert conn.execute('SELECT elapsed FROM live_match_statistics_75').fetchone()==(74,)

    if variant=='ambiguous':
        duplicate_archived_context(conn,context_id)
    elif variant=='too_old':
        conn.execute("""UPDATE api_observations SET requested_at=requested_at-interval '31 seconds',
            observed_at=observed_at-interval '31 seconds' WHERE observation_id=%s""",(context_id,))
    elif variant=='different_fixture':
        conn.execute("""UPDATE api_observations SET payload=jsonb_set(payload,'{response,0,fixture,id}','999')
            WHERE observation_id=%s""",(context_id,))
    elif variant=='different_run':
        other = conn.execute("""INSERT INTO collection_runs(job_name,status) VALUES('live_alerts','success')
            RETURNING run_id""").fetchone()[0]
        conn.execute('UPDATE api_observations SET run_id=%s WHERE observation_id=%s',(other,context_id))
    elif variant=='non_live_run':
        conn.execute("""UPDATE collection_runs SET job_name='fixtures' WHERE run_id=(
            SELECT run_id FROM api_observations WHERE observation_id=%s)""",(context_id,))
    elif variant=='outside_window':
        conn.execute("""UPDATE api_observations SET payload=jsonb_set(payload,'{response,0,fixture,status,elapsed}','76')
            WHERE observation_id=%s""",(context_id,))
    elif variant=='different_team':
        conn.execute("""UPDATE api_observations SET payload=jsonb_set(payload,'{response,0,teams,away,id}','999')
            WHERE observation_id=%s""",(context_id,))
    elif variant=='started_before_signal':
        conn.execute('UPDATE api_observations SET requested_at=%s WHERE observation_id=%s',
                     (NOW-timedelta(seconds=1),context_id))

    sent = []
    dispatch_reminders(conn,lambda *a:sent.append(a) or 999,now_fn=lambda:at)
    state,evidence = conn.execute('SELECT state,first_decision FROM live_signal_reminders').fetchone()
    if variant=='unique':
        assert (state,evidence['observation_id'],len(sent))==('sent',observation_id,1)
    else:
        assert (state,evidence,len(sent))==('waiting',None,0)


@DB
def test_explicit_binding_wins_and_is_not_duplicated(conn):
    from src.jobs.alert_dispatch import rows
    from src.jobs.signal_reminder_history import available_polls

    configure(conn,tracking=True,reminders=True,now=NOW)
    archive_api_poll(conn,NOW,67,6)
    assert run(conn)['sent']==1
    at = NOW+timedelta(minutes=1)
    observation_id = archive_api_poll(conn,at,68,6)
    context_id = conn.execute("""SELECT context_observation_id FROM live_api_reminder_observations
        WHERE observation_id=%s""",(int(observation_id),)).fetchone()[0]
    duplicate_archived_context(conn,context_id)

    job = rows(conn,'SELECT t.*,r.first_decision FROM live_signal_tracking t JOIN live_signal_reminders r USING (signal_id)')[0]
    polls = available_polls(conn,job,at)
    assert [(p['statistics_observation_id'],p['statistics_poll_id']) for p in polls]==[
        (observation_id,str(context_id))]
    assert dispatch_reminders(conn,lambda *a:999,now_fn=lambda:at)=={'reminder_sent':1}


@DB
@pytest.mark.parametrize('variant', [
    'duplicate', 'missing', 'wrong_team', 'after_statistics_started', 'api_error', 'explicit',
])
def test_api_archive_uses_latest_successful_context_before_statistics(conn, variant):
    from psycopg.types.json import Jsonb
    from src.jobs.alert_dispatch import rows
    from src.jobs.signal_reminder_history import available_polls

    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 74, 6)
    assert run(conn)['sent'] == 1
    at = NOW + timedelta(minutes=1)
    observation_id = archive_api_poll(conn, at, 73, 6)
    context_id = conn.execute("""
        SELECT context_observation_id FROM live_api_reminder_observations WHERE observation_id=%s
    """, (observation_id,)).fetchone()[0]
    if variant != 'explicit':
        conn.execute('DELETE FROM live_api_reminder_observations WHERE observation_id=%s', (observation_id,))
    assert conn.execute('SELECT elapsed FROM live_match_statistics_75').fetchone() == (74,)

    run_id, payload = conn.execute("""
        SELECT run_id,payload FROM api_observations WHERE observation_id=%s
    """, (context_id,)).fetchone()
    match = payload['response'][0]
    if variant == 'missing':
        payload['response'] = []
    elif variant == 'wrong_team':
        match['teams']['away']['id'] = 999
    elif variant == 'api_error':
        payload['errors'] = ['timeout']
    else:
        payload['response'] = [match, match]
    updated_at = at - timedelta(milliseconds=500 if variant == 'after_statistics_started' else 1500)
    conn.execute("""
        INSERT INTO api_observations(run_id,endpoint,params,requested_at,observed_at,payload)
        VALUES (%s,'/fixtures','{"live":"all"}',%s,%s,%s)
    """, (run_id, updated_at - timedelta(milliseconds=100), updated_at, Jsonb(payload)))

    job = rows(conn, """
        SELECT t.*,r.first_decision FROM live_signal_tracking t JOIN live_signal_reminders r USING (signal_id)
    """)[0]
    sent = []
    observations_before = conn.execute('SELECT count(*) FROM api_observations').fetchone()
    polls = available_polls(conn, job, at)
    dispatch_reminders(conn, lambda *args: sent.append(args) or 999, now_fn=lambda: at)
    assert conn.execute('SELECT count(*) FROM api_observations').fetchone() == observations_before
    state, evidence = conn.execute('SELECT state,first_decision FROM live_signal_reminders').fetchone()

    if variant in ('after_statistics_started', 'api_error', 'explicit'):
        assert [(p['statistics_observation_id'], p['statistics_poll_id']) for p in polls] == [
            (observation_id, str(context_id)),
        ]
        assert (state, evidence['observation_id'], len(sent)) == ('sent', observation_id, 1)
    else:
        assert not polls
        assert (state, evidence, len(sent)) == ('waiting', None, 0)
        next_at = at + timedelta(minutes=1)
        next_id = archive_api_poll(conn, next_at, 74, 6)
        assert dispatch_reminders(conn, lambda *args: sent.append(args) or 999,
                                  now_fn=lambda: next_at) == {'reminder_sent': 1}
        decision_id, = conn.execute("SELECT first_decision->>'observation_id' FROM live_signal_reminders").fetchone()
        assert (decision_id, len(sent)) == (next_id, 1)


@DB
@pytest.mark.parametrize('positive_decision', [False, True])
def test_duplicate_api_context_cannot_authorize_reminder_via_retained_snapshot(conn, tmp_path, positive_decision):
    from datetime import datetime
    from src.jobs.collect import collection_run
    from src.jobs.live_context import record_api_context
    from src.jobs.live_statistics import collect_statistics
    from tests.test_collect import make_client
    from tests.test_live_alerts import live_item
    from tests.test_live_statistics import body

    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 74, 6)
    assert run(conn)['sent'] == 1
    first_id = None
    if positive_decision:
        first_at = NOW + timedelta(minutes=1)
        first_id = archive_api_poll(conn, first_at, 74, 6)
        assert dispatch_reminders(conn, lambda *args: pytest.fail('delivery budget exhausted'),
                                  now_fn=lambda: first_at, clock=lambda: 80, deadline=85) == {
            'reminder_budget_deferred': 1,
        }

    client = make_client(tmp_path, [
        {'response': [live_item(1, minute=74)]},
        {'response': [live_item(1, minute=74), live_item(1, minute=75)]},
        body(6, 0),
    ])
    fake_now = client.now
    start = (NOW + timedelta(minutes=5)).timestamp()
    client.now = lambda: start + fake_now()
    with collection_run(conn, client, 'live_alerts') as collection:
        live = client.get('/fixtures', {'live': 'all'}, use_cache=False)
        context = client.last_observation
        record_api_context(conn, live['response'], context)
        client.sleep(24)
        collect_statistics(client, conn, live['response'], context, collection.run_id)
    at = datetime.fromtimestamp(client.last_observation.observed_at, NOW.tzinfo)
    snapshot = (75, client.last_observation.observation_id)
    count_before = conn.execute('SELECT count(*) FROM api_observations').fetchone()

    sent = []
    send = lambda *args: sent.append(args) or 999
    assert dispatch_reminders(conn, send, now_fn=lambda: at) == {}
    assert conn.execute("""
        SELECT state,first_decision->>'observation_id',attempts FROM live_signal_reminders
    """).fetchone() == ('waiting', first_id, 0)
    assert conn.execute('SELECT elapsed,observation_id FROM live_match_statistics_75').fetchone() == snapshot
    assert conn.execute('SELECT count(*) FROM api_observations').fetchone() == count_before
    assert client.requests_used == 3
    assert sent == []

    next_at = at + timedelta(minutes=1)
    next_id = archive_api_poll(conn, next_at, 75, 6)
    # The snapshot's archival reference is the only explicit link for this poll.
    conn.execute('DELETE FROM live_api_reminder_observations WHERE observation_id=%s', (next_id,))
    assert dispatch_reminders(conn, send, now_fn=lambda: next_at) == {'reminder_sent': 1}
    assert dispatch_reminders(conn, send, now_fn=lambda: next_at) == {}
    assert conn.execute("""
        SELECT state,first_decision->>'observation_id',attempts FROM live_signal_reminders
    """).fetchone() == ('sent', first_id or next_id, 1)
    assert len(sent) == 1


def archive_live_response(conn, tmp_path, at, matches):
    from src.jobs.collect import collection_run
    from src.jobs.live_context import record_api_context
    from tests.test_collect import make_client

    client = make_client(tmp_path, [{'response': matches}])
    fake_now = client.now
    client.now = lambda: at.timestamp() + fake_now()
    with collection_run(conn, client, 'live_alerts'):
        live = client.get('/fixtures', {'live': 'all'}, use_cache=False)
        record_api_context(conn, live['response'], client.last_observation)


@DB
@pytest.mark.parametrize('minute,status,duplicate,next_action', [
    (75, '2H', True, 'sent'),
    (76, '2H', True, 'outside_window'),
    (90, 'FT', True, 'regulation_end_or_void'),
    (75, '2H', True, 'window_expired'),
    (75, '2H', False, 'sent'),
])
def test_api_reminder_validates_refreshed_context_before_sending_or_closing(
        conn, tmp_path, minute, status, duplicate, next_action):
    from datetime import datetime
    from src.jobs.collect import collection_run
    from src.jobs.live_context import record_api_context
    from src.jobs.live_statistics import collect_statistics
    from tests.test_collect import make_client
    from tests.test_live_alerts import live_item
    from tests.test_live_statistics import body

    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 72, 6)
    assert run(conn)['sent'] == 1

    other = live_item(2, minute=73)
    other['teams'] = {'home': {'id': 30, 'name': 'Team C'}, 'away': {'id': 40, 'name': 'Team D'}}
    other_stats = body(0, 0)
    for team_id, row in zip((30, 40), other_stats['response']):
        row['team']['id'] = team_id
    latest = live_item(1, minute=minute, status=status)
    refreshed = [latest, dict(live_item(2, minute=74), teams=other['teams'])]
    if duplicate:
        refreshed.insert(0, live_item(1, minute=74))
    client = make_client(tmp_path, [
        {'response': [live_item(1, minute=73), other]}, body(6, 0), {'response': refreshed}, other_stats,
    ])
    fake_now = client.now
    starts_at = NOW + timedelta(minutes=1)
    client.now = lambda: starts_at.timestamp() + fake_now()
    scripted_transport = client.transport

    def delay_first_statistics(*args):
        response = scripted_transport(*args)
        if len(scripted_transport.calls) == 2:
            client.sleep(24)
        return response

    client.transport = delay_first_statistics
    with collection_run(conn, client, 'live_alerts') as collection:
        live = client.get('/fixtures', {'live': 'all'}, use_cache=False)
        context = client.last_observation
        record_api_context(conn, live['response'], context)
        collect_statistics(conn=conn, client=client, items=live['response'],
                           context=context, run_id=collection.run_id)

    at = datetime.fromtimestamp(client.last_observation.observed_at, NOW.tzinfo)
    retained = conn.execute("""
        SELECT elapsed,observation_id::text FROM live_match_statistics_75 WHERE fixture_id=1
    """).fetchone()
    assert retained[0] == 73
    assert conn.execute("""
        SELECT elapsed,status FROM live_alert_context WHERE source='api_football' AND source_event_id='1'
    """).fetchone() == (minute, status)
    archive_count = conn.execute('SELECT count(*) FROM api_observations').fetchone()
    sent = []

    def send(*args):
        sent.append(args)
        return 999

    dispatch_reminders(conn, send, now_fn=lambda: at)
    assert conn.execute("""
        SELECT state,reason,first_decision->>'observation_id',attempts FROM live_signal_reminders
    """).fetchone() == ('waiting' if duplicate else 'sent', None, retained[1], int(not duplicate))
    assert client.requests_used == 4
    assert conn.execute('SELECT count(*) FROM api_observations').fetchone() == archive_count

    next_at = at + timedelta(minutes=1)
    if duplicate and next_action == 'sent':
        archive_api_poll(conn, next_at, 75, 6)
    elif next_action == 'window_expired':
        next_at = NOW + timedelta(minutes=20)
    elif next_action != 'sent':
        archive_live_response(conn, tmp_path/'next', next_at, [latest])
    dispatch_reminders(conn, send, now_fn=lambda: next_at)
    assert conn.execute("""
        SELECT state,reason,first_decision->>'observation_id',attempts FROM live_signal_reminders
    """).fetchone() == (
        'sent' if next_action == 'sent' else 'cancelled',
        None if next_action == 'sent' else next_action,
        retained[1],
        int(next_action == 'sent'),
    )
    assert len(sent) == int(next_action == 'sent')


@DB
def test_api_alias_must_have_unambiguous_evidence_to_close_fonbet_reminder(conn, tmp_path):
    from tests.test_collect import seed_league_and_fixture
    from tests.test_live_alerts import live_item

    seed(conn)
    seed_league_and_fixture(conn, fixture_id=1)
    conn.execute("SELECT live_alert_link('123',1)")
    at = NOW + timedelta(minutes=4)
    archive_live_response(conn, tmp_path/'mismatch', at, [live_item(1, minute=90, status='FT')])
    assert dispatch_reminders(conn, lambda *a: pytest.fail('wrong API alias teams'),
                              now_fn=lambda: at) == {}

    at += timedelta(minutes=1)
    terminal = live_item(1, minute=90, status='FT')
    terminal['teams']['home']['id'], terminal['teams']['away']['id'] = (36, 34)
    duplicate = dict(live_item(1, minute=75), teams=terminal['teams'])
    archive_live_response(conn, tmp_path/'ambiguous', at, [duplicate, terminal])
    assert dispatch_reminders(conn, lambda *a: pytest.fail('no fresh Fonbet poll'),
                              now_fn=lambda: at) == {}
    assert conn.execute('SELECT state FROM live_signal_reminders').fetchone() == ('waiting',)

    next_at = at + timedelta(minutes=1)
    archive_live_response(conn, tmp_path/'verified', next_at, [terminal])
    assert dispatch_reminders(conn, lambda *a: pytest.fail('match ended'),
                              now_fn=lambda: next_at) == {'reminder_cancelled': 1}
    assert conn.execute('SELECT state,reason FROM live_signal_reminders').fetchone() == (
        'cancelled', 'regulation_end_or_void',
    )


@DB
def test_api_current_context_is_validated_again_after_ready_queue_selection(conn, tmp_path):
    from tests.test_live_alerts import live_item

    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 72, 6)
    assert run(conn)['sent'] == 1
    ready_at = NOW + timedelta(minutes=1)
    first_id = archive_api_poll(conn, ready_at, 73, 6)
    updated_at = ready_at + timedelta(seconds=1)
    ticks = iter((ready_at, updated_at))

    def now():
        at = next(ticks)
        if at == updated_at:
            archive_live_response(conn, tmp_path, at, [live_item(1, minute=75), live_item(1, minute=76)])
        return at

    assert dispatch_reminders(conn, lambda *a: pytest.fail('ambiguous live refresh'),
                              now_fn=now) == {}
    assert next(ticks, None) is None
    assert conn.execute("""
        SELECT state,first_decision->>'observation_id',attempts FROM live_signal_reminders
    """).fetchone() == ('waiting', first_id, 0)


@DB
@pytest.mark.parametrize('minute,status,reason', [
    (76, '2H', 'outside_window'), (90, 'FT', 'regulation_end_or_void'),
])
def test_api_terminal_context_must_match_original_teams(conn, tmp_path, minute, status, reason):
    from tests.test_live_alerts import live_item

    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 72, 6)
    assert run(conn)['sent'] == 1
    first_id = archive_api_poll(conn, NOW + timedelta(minutes=1), 73, 6)
    current = live_item(1, minute=minute, status=status)
    incorrect = dict(current, teams={'home': {'id': 30}, 'away': {'id': 40}})
    at = NOW + timedelta(minutes=2)
    archive_live_response(conn, tmp_path/'incorrect', at, [incorrect])
    assert dispatch_reminders(conn, lambda *a: pytest.fail('mismatched teams'),
                              now_fn=lambda: at) == {}
    assert conn.execute("""
        SELECT state,reason,first_decision->>'observation_id' FROM live_signal_reminders
    """).fetchone() == ('waiting', None, first_id)

    at += timedelta(minutes=1)
    archive_live_response(conn, tmp_path/'correct', at, [current])
    assert dispatch_reminders(conn, lambda *a: pytest.fail('valid end context'),
                              now_fn=lambda: at) == {'reminder_cancelled': 1}
    assert conn.execute("""
        SELECT state,reason,first_decision->>'observation_id',attempts FROM live_signal_reminders
    """).fetchone() == ('cancelled', reason, first_id, 0)


@DB
def test_mismatched_archived_teams_do_not_decide_or_send_api_reminder(conn):
    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 72, 6)
    assert run(conn)['sent'] == 1
    at = NOW + timedelta(minutes=1)
    archive_api_poll(conn, at, 73, 5, team_ids=(30, 40))
    at += timedelta(minutes=1)
    archive_api_poll(conn, at, 74, 6, team_ids=(20, 10))
    assert dispatch_reminders(conn, lambda *a: pytest.fail('mismatched teams'),
                              now_fn=lambda: at) == {}
    assert conn.execute('SELECT state,first_decision FROM live_signal_reminders').fetchone() == ('waiting', None)

    at += timedelta(minutes=1)
    valid = archive_api_poll(conn, at, 75, 6)
    assert dispatch_reminders(conn, lambda *a: 999, now_fn=lambda: at) == {'reminder_sent': 1}
    assert dispatch_reminders(conn, lambda *a: pytest.fail('duplicate reply'), now_fn=lambda: at) == {}
    assert conn.execute("""
        SELECT first_decision->>'observation_id',attempts FROM live_signal_reminders
    """).fetchone() == (valid, 1)


@DB
@pytest.mark.parametrize('previous_decision', [False, True])
def test_legacy_api_reminder_can_use_prior_decision_as_team_evidence(conn, tmp_path, previous_decision):
    from tests.test_live_alerts import live_item

    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 72, 6)
    assert run(conn)['sent'] == 1
    first_at = NOW + timedelta(minutes=1)
    first_id = archive_api_poll(conn, first_at, 73, 6)
    if previous_decision:
        assert dispatch_reminders(conn, lambda *a: pytest.fail('deferred by time budget'),
                                  now_fn=lambda: first_at, clock=lambda: 80, deadline=85) == {
            'reminder_budget_deferred': 1,
        }
    conn.execute("UPDATE live_signal_tracking SET first_context=first_context-'statistics_poll_id'")

    at = first_at + timedelta(minutes=1)
    archive_live_response(conn, tmp_path, at, [live_item(1, minute=76)])
    dispatch_reminders(conn, lambda *a: pytest.fail('no delivery'), now_fn=lambda: at)
    assert conn.execute("""
        SELECT state,reason,first_decision->>'observation_id' FROM live_signal_reminders
    """).fetchone() == (
        ('cancelled', 'outside_window', first_id) if previous_decision else ('waiting', None, None)
    )
    if not previous_decision:
        assert dispatch_reminders(conn, lambda *a: pytest.fail('deadline'),
                                  now_fn=lambda: NOW + timedelta(minutes=20)) == {'reminder_cancelled': 1}
        assert conn.execute('SELECT reason FROM live_signal_reminders').fetchone() == ('window_expired',)


@DB
@pytest.mark.parametrize('next_outcome', ['regulation_end_or_void', 'window_expired'])
def test_stale_fonbet_terminal_alias_cannot_cancel_reminder(conn, next_outcome):
    from src.jobs.signal_goal_store import save_timeline
    from src.jobs.signal_goals import Timeline
    from tests.test_signal_results import live_context

    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 72, 6)
    assert run(conn)['sent'] == 1
    fb(conn)
    match_id, = conn.execute("SELECT live_alert_link('123',1)").fetchone()
    live_at = NOW + timedelta(minutes=10)
    with conn.transaction():
        save_timeline(conn, match_id, 'fonbet', '123',
                      Timeline(status='2H', elapsed=74, period='regulation_2h'), live_at, {})
    live_context(conn, 'FT', 90, live_at - timedelta(seconds=20))
    assert dispatch_reminders(conn, lambda *a: pytest.fail('no API poll to send'),
                              now_fn=lambda: live_at + timedelta(seconds=5)) == {}
    assert conn.execute('SELECT state,attempts FROM live_signal_reminders').fetchone() == ('waiting', 0)
    if next_outcome == 'regulation_end_or_void':
        next_at = live_at + timedelta(minutes=1)
        live_context(conn, 'FT', 90, next_at - timedelta(seconds=1))
    else:
        next_at = NOW + timedelta(minutes=20)
    assert dispatch_reminders(conn, lambda *a: pytest.fail('terminal or expired'),
                              now_fn=lambda: next_at) == {'reminder_cancelled': 1}
    assert conn.execute('SELECT state,reason,attempts FROM live_signal_reminders').fetchone() == (
        'cancelled', next_outcome, 0,
    )


@DB
@pytest.mark.parametrize('browser_minute,regular_offset,expected_state', [
    (76, -35, 'cancelled'),
    (76, 1, 'cancelled'),  # The regular context shares the browser timestamp.
    (76, 10, 'sent'),
    (75, -35, 'sent'),
])
def test_fonbet_browser_clock_guards_reminder_window(conn, browser_minute, regular_offset, expected_state):
    from src.jobs.signal_goals import Timeline
    from src.jobs.signal_results import run_results

    seed(conn)
    browser_at = NOW + timedelta(minutes=4)
    regular_at = browser_at + timedelta(seconds=regular_offset)
    fresh_fonbet(conn, at=min(regular_at, browser_at - timedelta(seconds=2)), minute=74)

    def read_browser(*args, **kwargs):
        timeline = Timeline(status='2H', elapsed=browser_minute, period='regulation_2h')
        return timeline, browser_at.isoformat(), {}

    assert run_results(conn, fonbet_loader=read_browser,
                       now_fn=lambda: browser_at) == {'waiting_end': 1}
    fresh_fonbet(conn, at=regular_at, minute=74)
    dispatch_at = max(regular_at, browser_at) + timedelta(seconds=3)
    sent = []
    dispatch_reminders(conn, lambda text, markup: sent.append(text) or 123, now_fn=lambda: dispatch_at)
    reason = 'outside_window' if expected_state == 'cancelled' else None
    assert conn.execute("""
        SELECT state,reason,(first_decision->>'triggered')::boolean,attempts FROM live_signal_reminders
    """).fetchone() == (expected_state, reason, True, len(sent))
    assert len(sent) == int(expected_state == 'sent')


@DB
def test_linked_fonbet_clock_can_close_api_reminder_before_sending(conn):
    from src.jobs.signal_goal_store import save_timeline
    from src.jobs.signal_goals import Timeline

    configure(conn, tracking=True, reminders=True, now=NOW)
    archive_api_poll(conn, NOW, 72, 6)
    assert run(conn)['sent'] == 1
    fb(conn)
    match_id, = conn.execute("SELECT live_alert_link('123',1)").fetchone()
    first_at = NOW + timedelta(minutes=1)
    first_id = archive_api_poll(conn, first_at, 74, 6)
    later = first_at + timedelta(seconds=10)
    ticks = iter((first_at, later))

    def now():
        at = next(ticks)
        if at == later:
            with conn.transaction():
                save_timeline(conn, match_id, 'fonbet', '123',
                              Timeline(status='2H', elapsed=76, period='regulation_2h'), at, {})
        return at

    assert dispatch_reminders(conn, lambda *a: pytest.fail('match outside the window'),
                              now_fn=now) == {'reminder_cancelled': 1}
    assert next(ticks, None) is None
    assert conn.execute("""
        SELECT state,reason,first_decision->>'observation_id',attempts FROM live_signal_reminders
    """).fetchone() == ('cancelled', 'outside_window', first_id, 0)
