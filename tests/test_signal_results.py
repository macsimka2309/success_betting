import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from src.jobs.signal_results import end_state,schedule_after,run_results
from src.jobs.signal_goals import Timeline,api_timeline
from src.jobs.signal_tracking import configure
from tests.test_signal_review_regressions import signal
from tests.test_collect import conn  # noqa: F401
from tests.test_alert_dispatch import NOW,api,fb,run,link
from tests.test_signal_goals import fixture,goal

DB=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='disposable DB required')

@pytest.mark.parametrize('status,expected',[('absent','waiting'),('2H','waiting'),('FT','ended'),('ET','ended'),('AET','ended'),('PEN','ended'),('ABD','void'),('CANC','void')])
def test_end_state(status,expected):assert end_state(status)==expected


def test_schedule_survives_downtime_without_consuming_missed_checks():
    signal=dict(regulation_end_confirmed_at=NOW,review_due_at=NOW+timedelta(days=1),review_day=0,primary_attempts=0)
    assert schedule_after(signal,NOW,True)==('preliminary',0,NOW+timedelta(days=1))
    assert schedule_after(signal,NOW,False)[2]==NOW+timedelta(minutes=5)
    late=NOW+timedelta(days=4)
    assert schedule_after(signal,late,False)==('review_due',1,late+timedelta(days=1))
    for day in range(1,8):
        signal['review_day']=day
        status,count,due=schedule_after(signal,late+timedelta(days=day),False)
        assert count==day+1
    assert status=='incomplete_final' and due is None
    signal['review_day']=1
    assert schedule_after(signal,late,True)[0]=='reviewed'


def seed(conn):
    configure(conn,tracking=True,reminders=False,now=NOW)
    fb(conn)
    return signal(conn,1,eid='123')


def goals_loader(events,score=(1,0)):
    def load(url,**kw):
        return api_timeline(fixture(events,score=score)),None,{}
    return load


@DB
def test_primary_and_mandatory_daily_review(conn):
    seed(conn)
    loader=goals_loader([goal(90,12)])
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW)['preliminary']==1
    assert conn.execute('SELECT timeline_complete,review_day FROM live_signal_tracking').fetchone()==(True,0)
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW+timedelta(hours=1))=={}
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW+timedelta(days=1))['reviewed']==1
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==1


@DB
def test_absence_is_not_finish_and_lease_recovers(conn):
    seed(conn)
    conn.execute('UPDATE live_signal_tracking SET lease_until=%s',(NOW+timedelta(minutes=1),))
    def loader(url,**kw):return Timeline(reason='live_event_missing'),None,{}
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW)=={}
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW+timedelta(minutes=2))['waiting_end']==1
    assert conn.execute('SELECT regulation_end_confirmed_at,live_phase FROM live_signal_tracking').fetchone()==(None,'unavailable')


@DB
def test_incomplete_keeps_events_through_seven_extra_reviews(conn):
    seed(conn)
    loader=goals_loader([goal()],score=(2,0))
    run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW)
    for day in range(4,12):
        at=NOW+timedelta(days=day)
        run_results(conn,fonbet_loader=loader,now_fn=lambda:at)
        assert run_results(conn,fonbet_loader=loader,now_fn=lambda:at)=={}
    assert conn.execute('SELECT status,review_day FROM live_signal_tracking').fetchone()==('incomplete_final',8)
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()[0]==1

@DB
def test_fonbet_only_finish_and_budgeted_read(conn):
    from tests.test_alert_dispatch import fb,run
    configure(conn,tracking=True,reminders=False,now=NOW)
    fb(conn);run(conn)
    seen=[]
    def loader(url,**kw):
        seen.append(url)
        return Timeline(status='FT',goals=[],complete=True,regulation_score=(0,0)),NOW.isoformat(),{'goals':[]}
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW)['preliminary']==1
    assert len(seen)==1
    assert conn.execute('SELECT regulation_end_confirmed_at FROM live_signal_tracking').fetchone()[0]==NOW

@DB
def test_primary_errors_do_not_lose_deadline_or_events(conn):
    seed(conn)
    loader=goals_loader([],score=(1,0))
    for minutes in (0,5,20,50,60):run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW+timedelta(minutes=minutes))
    assert conn.execute('SELECT primary_attempts,next_attempt_at FROM live_signal_tracking').fetchone()==(5,NOW+timedelta(days=1))

@DB
def test_disabled_worker_never_calls_sources(conn):
    assert run_results(conn,fonbet_loader=lambda *a,**k:pytest.fail('disabled collection'))=={'disabled':1}

@DB
def test_api_first_never_polls_results_even_when_mapped_to_fonbet(conn):
    configure(conn,tracking=True,reminders=False,now=NOW)
    api(conn);fb(conn);link(conn)
    assert run(conn)['sent']==1
    assert conn.execute('SELECT first_source,goals_enabled FROM live_signal_tracking').fetchone()==('api_football',False)
    assert run_results(conn,fonbet_loader=lambda *a,**k:pytest.fail('API signal entered results queue'),
                       now_fn=lambda:NOW+timedelta(minutes=45))=={}
    assert conn.execute('SELECT count(*) FROM live_signal_check_attempts').fetchone()==(0,)


def test_timer_configuration_and_wrapper_keep_existing_limits():
    import configparser
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    service=configparser.ConfigParser(interpolation=None);service.read(root/'deploy/football-signal-results.service')
    timer=configparser.ConfigParser(interpolation=None);timer.read(root/'deploy/football-signal-results.timer')
    assert service['Service']['TimeoutStartSec']=='150' and service['Service']['Nice']=='10'
    assert '--scheduled' in service['Service']['ExecStart']
    assert '--browser-limit 1' in service['Service']['ExecStart']
    assert timer['Timer']['OnCalendar']=='*-*-* *:*:45 Europe/Moscow'
    script=(root/'deploy/run_fonbet_goals.sh').read_text()
    assert 'flock -n -E 75 /profile/fonbet-browser.lock' in script
    assert 'browser_lock.py' in script and 'docker start' not in script

@DB
def test_operator_diagnostics_exposes_state_and_due(conn):
    from pathlib import Path
    mid=seed(conn)
    conn.execute("UPDATE live_signal_tracking SET status='incomplete_final',incomplete_reason='timeline_unavailable',last_attempt_at=%s,next_attempt_at=NULL",(NOW,))
    conn.execute("INSERT INTO live_signal_reminders(signal_id,state,reason) VALUES(1,'uncertain','delivery_unknown')")
    sql=(Path(__file__).resolve().parents[1]/'scripts/diagnostics/signal_tracking_status.sql').read_text()
    cur=conn.execute(sql)
    row=dict(zip([c.name for c in cur.description],cur.fetchone()))
    assert row['status']=='incomplete_final' and row['reminder_state']=='uncertain'
    assert row['last_attempt_at']==NOW and row['next_attempt_at'] is None
    assert row['incomplete_reason']=='timeline_unavailable'

@DB
def test_confirmed_end_cancels_reminder_even_after_live_disappearance(conn):
    seed(conn)
    conn.execute('INSERT INTO live_signal_reminders(signal_id) VALUES(1)')
    run_results(conn,fonbet_loader=goals_loader([],score=(0,0)),now_fn=lambda:NOW)
    assert conn.execute('SELECT state,reason FROM live_signal_reminders').fetchone()==('cancelled','regulation_end_confirmed')

@DB
def test_completed_result_evidence_starts_fonbet_only_schedule(conn):
    from tests.test_alert_dispatch import fb,run
    from pathlib import Path
    from src.jobs.signal_goals import load_fonbet_timeline
    import json
    configure(conn,tracking=True,reminders=False,now=NOW)
    fb(conn,eid='69626793');run(conn)
    html=(Path(__file__).parent/'fixtures/fonbet/completed-result.html').read_text()
    def loader(url,**kw):
        return load_fonbet_timeline(url,runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps({'completion_html':html})),**kw)
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:NOW)['preliminary']==1
    evidence=conn.execute('SELECT end_context,review_due_at FROM live_signal_tracking').fetchone()
    assert evidence[0]['evidence']['label']=='Завершенные'
    assert evidence[1]==NOW+timedelta(days=1)

@DB
def test_browser_busy_defers_without_any_api_dependency(conn,monkeypatch):
    from tests.test_signal_lifecycle import first_signal
    first_signal(conn)
    from src.api.client import ApiClient
    monkeypatch.setattr(ApiClient,'get',lambda *a,**k:pytest.fail('targeted API call'))
    calls=[]
    def busy(url,**kw):
        calls.append(url);return Timeline(reason='browser_busy',attempted=False),None,{}
    assert run_results(conn,fonbet_loader=busy,now_fn=lambda:NOW)['deferred_internal']==1
    assert conn.execute('SELECT primary_attempts,review_day,pending_api_observation_id FROM live_signal_tracking').fetchone()[:2]==(0,0)
    def available(url,**kw):
        calls.append(url);return Timeline(status='FT',complete=True,regulation_score=(0,0)),None,{}
    assert run_results(conn,fonbet_loader=available,now_fn=lambda:NOW+timedelta(minutes=5))['preliminary']==1
    assert len(calls)==2
    assert conn.execute('SELECT pending_api_observation_id,deferred_reason FROM live_signal_tracking').fetchone()==(None,None)

@DB
def test_no_source_contact_preserves_daily_count_and_next_slot(conn):
    from tests.test_signal_review_regressions import signal
    from tests.test_live_source_matching import add_fonbet
    configure(conn,tracking=True,reminders=False,now=NOW)
    mid=signal(conn,1,review=True,eid='123');add_fonbet(conn)
    conn.execute("INSERT INTO live_alert_aliases VALUES('fonbet','123',%s)",(mid,))
    def memory(url,**kw):return Timeline(reason='browser_memory_budget',attempted=False),None,{}
    at=NOW+timedelta(days=4)
    assert run_results(conn,fonbet_loader=memory,now_fn=lambda:at)['deferred_internal']==1
    assert conn.execute('SELECT review_day,last_attempt_at,next_attempt_at FROM live_signal_tracking').fetchone()==(0,None,at.replace(second=0)+timedelta(minutes=5))
    def error(url,**kw):return Timeline(reason='source_unavailable',attempted=True),None,{}
    assert run_results(conn,fonbet_loader=error,now_fn=lambda:at+timedelta(minutes=5))['review_due']==1
    assert conn.execute('SELECT review_day FROM live_signal_tracking').fetchone()[0]==1

@DB
def test_diagnostics_distinguishes_deferred_from_real_attempt(conn):
    from pathlib import Path
    seed(conn)
    conn.execute("UPDATE live_signal_tracking SET last_poll_at=%s,deferred_reason='browser_budget',next_attempt_at=%s",(NOW,NOW+timedelta(minutes=5)))
    cur=conn.execute((Path(__file__).resolve().parents[1]/'scripts/diagnostics/signal_tracking_status.sql').read_text())
    row=dict(zip([c.name for c in cur.description],cur.fetchone()))
    assert row['last_attempt_at'] is None and row['last_poll_at']==NOW
    assert row['deferred_reason']=='browser_budget' and row['remaining_daily_checks']==8


def live_context(conn, status, minute, at, eid='123'):
    conn.execute("""UPDATE live_alert_context SET status=%s,elapsed=%s,
      period=%s,poll_id=%s,present=%s,observed_at=%s,completed_at=%s,requested_at=%s
      WHERE source='fonbet' AND source_event_id=%s""",
      (status,minute,'regulation_2h' if status=='2H' else 'unknown',
       at.isoformat(),status!='absent',at,at,at-timedelta(seconds=1),eid))


@DB
@pytest.mark.parametrize('terminal',['FT','ET'])
def test_fonbet_89_stoppage_to_end_survives_restart(conn,terminal):
    seed(conn)
    tick=NOW.replace(second=45)+timedelta(minutes=9)
    seen=[]
    def loader(url,**kw):
        seen.append(kw['regulation_end_confirmed'])
        return Timeline(status='2H',period='regulation_2h',elapsed=90,added_minute=12,
                        goals=api_timeline(fixture([goal(90,12)])).goals),None,{'score':[1,0]}
    def poll(at,source=loader):
        return run_results(conn,fonbet_loader=source,now_fn=lambda:at,scheduled=True)
    assert poll(tick)=={}  # No ordinary work outside the five-minute cadence.
    live_context(conn,'2H',89,tick-timedelta(seconds=5))
    assert poll(tick)['waiting_end']==1
    assert poll(tick+timedelta(seconds=4))=={}
    assert poll(tick+timedelta(minutes=1))['waiting_end']==1
    stored=conn.execute("""SELECT t.last_live_slot,t.review_day,
      o.payload->'timeline'->>'added_minute' FROM live_signal_tracking t
      JOIN live_signal_goal_observations o ON o.observation_id=t.last_live_observation_id""").fetchone()
    assert stored==(tick.replace(second=0)+timedelta(minutes=1),0,'12')
    missing=tick+timedelta(minutes=2)
    assert poll(missing,lambda *a,**k:(Timeline(reason='live_event_missing'),None,{}))['waiting_end']==1
    assert conn.execute('SELECT live_phase,regulation_end_confirmed_at FROM live_signal_tracking').fetchone()==('unavailable',None)
    assert poll(missing+timedelta(minutes=1))=={}
    ended=tick+timedelta(minutes=6)
    live_context(conn,terminal,90,ended-timedelta(seconds=3))
    complete=goals_loader([goal(90,12)])
    assert poll(ended,complete)['preliminary']==1
    assert poll(ended+timedelta(days=1),complete)['reviewed']==1
    assert conn.execute("SELECT count(*) FROM live_signal_goals WHERE active AND minute=90 AND added_minute=12").fetchone()==(1,)
    assert seen==[False,False]


@DB
def test_minute_browser_budget_rotates_and_never_spends_reviews(conn):
    seed(conn)
    fb(conn,eid='124')
    signal(conn,2,eid='124')
    tick=NOW+timedelta(minutes=9)
    for eid in ('123','124'):
        live_context(conn,'2H',89,tick-timedelta(seconds=2),eid)
    seen=[]
    def load(url,**kw):
        seen.append(url.rsplit('/',1)[1])
        return Timeline(status='2H',elapsed=89,period='regulation_2h'),None,{}
    assert run_results(conn,fonbet_loader=load,now_fn=lambda:tick,scheduled=True)=={'waiting_end':1,'deferred_internal':1}
    run_results(conn,fonbet_loader=load,now_fn=lambda:tick+timedelta(minutes=1),scheduled=True)
    assert seen==['123','124']
    assert conn.execute('SELECT sum(missed_live_slots),sum(review_day),sum(primary_attempts) FROM live_signal_tracking').fetchone()==(2,0,0)


@DB
def test_result_without_timeline_compares_last_live_score(conn):
    seed(conn)
    end=NOW+timedelta(minutes=5)
    live=api_timeline(fixture([goal(90,12)]))
    live.complete=False
    live.status='2H'
    live.period='regulation_2h'
    live.elapsed=90
    live.added_minute=12
    payload={'score':[1,0],'parsed_regulation_score':[1,0],'teams':['Home','Away']}
    run_results(conn,fonbet_loader=lambda *a,**k:(live,None,payload),now_fn=lambda:NOW)
    completed={'source_event_id':'123','regulation_score':[0,1],'teams':['Home','Away']}
    def result(url,**kw):
        return Timeline(status='REG_END',reason='timeline_unavailable'),None,{'completion_evidence':completed}
    assert run_results(conn,fonbet_loader=result,now_fn=lambda:end)['preliminary']==1
    assert conn.execute("""SELECT incomplete_reason,timeline_complete,last_live_observation_id IS NOT NULL
      FROM live_signal_tracking""").fetchone()==('regulation_score_mismatch',False,True)
    assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone()==(1,)


@DB
def test_regular_absence_pauses_minute_slots_after_context_expires(conn):
    seed(conn)
    tick=NOW+timedelta(minutes=9)
    live_context(conn,'2H',89,tick-timedelta(seconds=2))
    def loader(*a,**k):
        return Timeline(status='2H',elapsed=89,period='regulation_2h'),None,{}
    run_results(conn,fonbet_loader=loader,now_fn=lambda:tick,scheduled=True)
    next_tick=tick+timedelta(minutes=2)
    live_context(conn,'absent',None,next_tick-timedelta(seconds=2))
    def unexpected(*a,**k):pytest.fail('an absent card gets only ordinary checks')
    assert run_results(conn,fonbet_loader=unexpected,now_fn=lambda:next_tick,scheduled=True)=={}
    assert run_results(conn,fonbet_loader=unexpected,now_fn=lambda:next_tick+timedelta(minutes=3),scheduled=True)=={}
    assert conn.execute('SELECT live_phase,regulation_end_confirmed_at FROM live_signal_tracking').fetchone()==('unavailable',None)


@DB
def test_void_from_regular_context_stops_fonbet_without_spending_io(conn):
    seed(conn)
    conn.execute('INSERT INTO live_signal_reminders(signal_id) VALUES(1)')
    tick=NOW+timedelta(minutes=10)
    live_context(conn,'CANC',None,tick-timedelta(seconds=2))
    def no_io(*a,**k):pytest.fail('void match polled the browser')
    assert run_results(conn,fonbet_loader=no_io,now_fn=lambda:tick,scheduled=True)=={'void':1}
    assert conn.execute("""SELECT status,regulation_end_confirmed_at,last_poll_at,
      end_context->>'status',primary_attempts,review_day FROM live_signal_tracking""").fetchone()==(
        'void',None,None,'CANC',0,0)
    assert conn.execute('SELECT state,reason FROM live_signal_reminders').fetchone()==('cancelled','match_void')


@DB
def test_distinct_stoppage_goals_are_projected_from_live_and_keep_ids(conn):
    import json
    from pathlib import Path
    from src.jobs.signal_goals import load_fonbet_timeline

    seed(conn)
    html = (Path(__file__).parent/'fixtures/fonbet/timeline-seven-goals.html').read_text()
    html = html.replace("79'", "90+2'").replace("84'", "90+6'")
    raw = dict(html=html, source_event_id='123', clock_text='90+6', period_text='2-й тайм')

    def loader(url,**kwargs):
        runner = lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(raw))
        return load_fonbet_timeline(url,runner=runner,**kwargs)

    def goals():
        return conn.execute("""SELECT goal_id,added_minute FROM live_signal_goals_selected
            WHERE minute=90 AND relation='after' ORDER BY added_minute""").fetchall()

    tick = NOW.replace(second=45)+timedelta(minutes=9)
    live_context(conn,'2H',89,tick-timedelta(seconds=3))
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:tick,scheduled=True)=={'waiting_end':1}
    first = goals()
    assert [minute for _,minute in first]==[2,6]
    assert conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone()==(0,)
    assert run_results(conn,fonbet_loader=loader,now_fn=lambda:tick+timedelta(minutes=1),
                       scheduled=True)=={'waiting_end':1}
    assert goals()==first
    assert conn.execute('SELECT timeline_complete FROM live_signal_tracking').fetchone()==(False,)

    def corrected(url,**kwargs):
        live = loader(url,**kwargs)[0]
        final = Timeline(goals=live.goals[:-1],complete=True,status='FT',regulation_score=(6,0))
        return final,None,{}

    assert run_results(conn,fonbet_loader=corrected,now_fn=lambda:tick+timedelta(minutes=2),
                       scheduled=True)=={'preliminary':1}
    assert goals()==first[:1]
    assert conn.execute('SELECT active FROM live_signal_goals WHERE goal_id=%s',(first[1][0],)).fetchone()==(False,)


@DB
@pytest.mark.parametrize('stage',['primary','daily'])
def test_post_match_receives_regular_slot_after_internal_deferral(conn,stage):
    tick = NOW.replace(second=45)+timedelta(days=1)
    configure(conn,tracking=True,reminders=False,now=NOW)
    fb(conn)
    signal(conn,1,when=tick-timedelta(minutes=24),eid='123')
    fb(conn,eid='124')
    signal(conn,2,review=True,eid='124')
    original = tick-timedelta(seconds=40)
    conn.execute("""UPDATE live_signal_tracking SET next_attempt_at=%s,result_due_at=%s
        WHERE signal_id=2""",(original,original))
    if stage=='primary':
        conn.execute("""UPDATE live_signal_tracking SET status='waiting_end',regulation_end_confirmed_at=%s,
            primary_due_at=%s,review_due_at=%s WHERE signal_id=2""",(original,original,original+timedelta(days=1)))
    calls = []
    post_allowed = False

    def loader(url,**kwargs):
        event = url.rsplit('/',1)[1]
        calls.append(event)
        if event=='123':
            return Timeline(status='2H',elapsed=90,added_minute=5,period='regulation_2h'),None,{}
        if not post_allowed:
            return Timeline(reason='browser_busy',attempted=False),None,{}
        return Timeline(status='REG_END',complete=True,regulation_score=(0,0)),None,{}

    def cycle(offset):
        at = tick+timedelta(minutes=offset)
        live_context(conn,'2H',89,at-timedelta(seconds=3))
        start = len(calls)
        run_results(conn,fonbet_loader=loader,now_fn=lambda:at,scheduled=True)
        assert len(calls)==start+1

    def state():
        return conn.execute("""SELECT result_due_at,deferred_reason,primary_attempts,review_day
            FROM live_signal_tracking WHERE signal_id=2""").fetchone()

    for minute in range(5):
        cycle(minute)
        assert calls[-1]=='123'
    assert state()==(original,'browser_budget',0,0)
    cycle(5)
    assert calls[-1]=='124'
    assert state()==(original,'browser_busy',0,0)
    cycle(6)
    assert calls[-1]=='123'
    post_allowed = True
    cycle(10)
    assert calls[-1]=='124'
    expected = (None,1,0) if stage=='primary' else (None,0,1)
    assert state()[1:]==expected


@DB
def test_post_match_priority_orders_overdue_checks_by_original_due(conn):
    tick = NOW.replace(second=45)+timedelta(days=2)
    configure(conn,tracking=True,reminders=False,now=NOW)
    for sid in (1,2):
        event = str(122+sid)
        fb(conn,eid=event)
        signal(conn,sid,review=True,eid=event)
        conn.execute("""UPDATE live_signal_tracking SET result_due_at=%s,next_attempt_at=%s,last_poll_at=%s
          WHERE signal_id=%s""",(tick-timedelta(minutes=sid*10),tick-timedelta(seconds=1),
                                tick-timedelta(minutes=(3-sid)*10),sid))
    seen = []
    def loader(url,**kwargs):
        seen.append(url.rsplit('/',1)[1])
        return Timeline(status='REG_END',complete=True,regulation_score=(0,0)),None,{}
    run_results(conn,fonbet_loader=loader,now_fn=lambda:tick,scheduled=True)
    assert seen==['124']
    assert conn.execute('SELECT review_day FROM live_signal_tracking ORDER BY signal_id').fetchall()==[(0,),(1,)]


def goal_at(minute, added, score):
    return {'period':'regulation_2h', 'credited_side':'home', 'minute':minute,
            'added_minute':added, 'score_after_home':score, 'score_after_away':0}


def read_goal_snapshot(conn, at, goals, *, valid=True, finished=False, receipt=None):
    score = tuple(sum(goal['credited_side'] == side for goal in goals) for side in ('home', 'away'))

    def loader(*args, **kwargs):
        timeline = Timeline(goals=goals, complete=finished, status='FT' if finished else '2H',
                            elapsed=90, added_minute=6, period='regulation_2h',
                            regulation_score=score if finished else None,
                            reason='timeline_incomplete' if not valid else None)
        return timeline,receipt,{'structure_valid':valid,'score':list(score)}
    return run_results(conn,now_fn=lambda:at,fonbet_loader=loader)


def selected_goals(conn):
    return conn.execute("""SELECT goal_id,minute,added_minute,score_after_home
        FROM live_signal_goals_selected ORDER BY score_after_home""").fetchall()


@DB
def test_partial_same_timestamp_with_a_different_score_cannot_overwrite_known_goal(conn):
    seed(conn)
    first = NOW+timedelta(minutes=20)
    assert read_goal_snapshot(conn,first,[goal_at(90,2,1)])=={'waiting_end':1}
    known = selected_goals(conn)
    assert len(known)==1
    partial = first+timedelta(minutes=1)
    for at in (partial,partial+timedelta(minutes=1)):
        assert read_goal_snapshot(conn,at,[goal_at(90,2,2)],valid=False,
                                  receipt=partial.isoformat())=={'waiting_end':1}
        assert selected_goals(conn)==known
    assert conn.execute("""SELECT candidate_goal_ids,payload->>'score_after_home',reason
        FROM live_signal_goal_pending""").fetchall()==[([known[0][0]],'2','identity_ambiguous')]
    assert conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone()==(1,)


@DB
@pytest.mark.parametrize('detail', [None, 'Гол'])
def test_complete_timeline_correction_preserves_both_goal_identities_at_same_minute(conn, detail):
    seed(conn)
    first = NOW+timedelta(minutes=20)
    original = [dict(goal_at(86,None,1), detail=detail), dict(goal_at(90,2,2), detail=detail)]
    assert read_goal_snapshot(conn,first,original)=={'waiting_end':1}
    known = selected_goals(conn)
    assert len(known)==2

    final = [dict(original[0], minute=90, added_minute=2), original[1]]
    ended = first+timedelta(minutes=1)
    expected = [(known[0][0],90,2,1),(known[1][0],90,2,2)]
    assert read_goal_snapshot(conn,ended,final,finished=True)=={'preliminary':1}
    assert selected_goals(conn)==expected
    assert read_goal_snapshot(conn,ended+timedelta(days=1),final,finished=True)=={'reviewed':1}
    assert selected_goals(conn)==expected
    assert conn.execute('SELECT count(*) FROM live_signal_goals').fetchone()==(2,)
    assert conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone()==(0,)


@DB
def test_known_fonbet_goal_keeps_its_id_when_same_minute_penalty_is_added(conn):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    open_play = dict(goal_at(90, 2, 1), detail='Гол')
    assert read_goal_snapshot(conn, first, [open_play]) == {'waiting_end': 1}
    known_id, = conn.execute('SELECT goal_id FROM live_signal_goals').fetchone()
    corrected = [dict(goal_at(90, 2, 1), detail='Пенальти забит'),
                 dict(open_play, score_after_home=2)]

    # A validated live read can see the correction before the final whistle.
    live_at = first + timedelta(minutes=1)
    assert read_goal_snapshot(conn, live_at, corrected) == {'waiting_end': 1}
    penalty_id, = conn.execute("SELECT goal_id FROM live_signal_goals WHERE source_detail='Пенальти забит'").fetchone()
    assert penalty_id != known_id
    expected = [(penalty_id, 90, 2, 1), (known_id, 90, 2, 2)]
    assert selected_goals(conn) == expected

    ended = live_at + timedelta(minutes=1)
    assert read_goal_snapshot(conn, ended, corrected, finished=True) == {'preliminary': 1}
    assert read_goal_snapshot(conn, ended + timedelta(days=1), corrected, finished=True) == {'reviewed': 1}
    assert selected_goals(conn) == expected
    assert conn.execute('SELECT count(*) FROM live_signal_goals').fetchone() == (2,)
    assert conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone() == (0,)
    assert conn.execute("""
        SELECT payload->>'score_after_home' FROM live_signal_goal_versions
        WHERE goal_id=%s ORDER BY observation_id
    """, (known_id,)).fetchall() == [('1',), ('2',), ('2',), ('2',)]


@DB
@pytest.mark.parametrize('first_detail', ['Пенальти забит', 'Гол'])
def test_same_minute_goals_keep_ids_after_an_earlier_goal_is_inserted(conn, first_detail):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    first_goal = dict(goal_at(90, 2, 1), detail=first_detail)
    second_goal = dict(goal_at(90, 2, 2), detail='Гол')
    assert read_goal_snapshot(conn, first, [first_goal, second_goal]) == {'waiting_end': 1}
    first_id, second_id = [row[0] for row in selected_goals(conn)]

    final = [dict(goal_at(88, None, 1), detail='Гол'),
             dict(first_goal, score_after_home=2), dict(second_goal, score_after_home=3)]
    ended = first + timedelta(minutes=1)
    for at, state in ((ended, 'preliminary'), (ended + timedelta(days=1), 'reviewed')):
        assert read_goal_snapshot(conn, at, final, finished=True) == {state: 1}
        early, late_first, late_second = conn.execute("""
            SELECT goal_id, minute, score_after_home, source_detail
            FROM live_signal_goals WHERE active ORDER BY score_after_home
        """).fetchall()
        assert early[0] not in (first_id, second_id)
        assert early[1:] == (88, 1, 'Гол')
        assert late_first == (first_id, 90, 2, first_detail)
        assert late_second == (second_id, 90, 3, 'Гол')

    assert conn.execute('SELECT count(*) FROM live_signal_goals').fetchone() == (3,)
    assert conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone() == (0,)
    assert conn.execute("""
        SELECT goal_id, payload->>'score_after_home'
        FROM live_signal_goal_versions WHERE goal_id IN (%s, %s)
        ORDER BY goal_id, observation_id
    """, (first_id, second_id)).fetchall() == [
        (first_id, '1'), (first_id, '2'), (first_id, '2'),
        (second_id, '2'), (second_id, '3'), (second_id, '3'),
    ]


@DB
def test_replayed_or_delayed_observation_preserves_newer_partial_projection(conn):
    from src.jobs.signal_goal_store import save_timeline

    seed(conn)
    first = NOW + timedelta(minutes=20)
    original = [
        dict(goal_at(70, None, 1), detail='Гол'),
        dict(goal_at(90, 2, 2), detail='Гол', source_goal_id='late-first'),
        dict(goal_at(90, 2, 3), detail='Гол'),
    ]
    assert read_goal_snapshot(conn, first, original) == {'waiting_end': 1}
    old_oid, match_id, source, event_id, snapshot = conn.execute("""
        SELECT observation_id,alert_match_id,source,source_event_id,payload
        FROM live_signal_goal_observations
    """).fetchone()
    corrected = [
        dict(goal_at(69, None, 0), detail='Гол', credited_side='away', score_after_away=1),
        dict(original[0], score_after_away=1),
        dict(goal_at(88, None, 2), detail='Гол', score_after_away=1),
        dict(original[2], score_after_away=1),
    ]
    ended = first + timedelta(minutes=1)
    assert read_goal_snapshot(conn, ended, corrected, finished=True) == {'preliminary': 1}

    def active_versions():
        return conn.execute("""
            SELECT goal_id,minute,score_after_away,observation_id,revision,active
            FROM live_signal_goals ORDER BY goal_id
        """).fetchall()

    applied = active_versions()
    earlier = next(row for row in applied if row[1] == 70)
    assert earlier[2] == 1
    versions = conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone()
    stale_inputs = [(first, snapshot['payload']), (first + timedelta(seconds=30), {'structure_valid': True})]
    for at, payload in stale_inputs:
        raw = Timeline(**snapshot['timeline'])
        with conn.transaction():
            replayed = save_timeline(conn, match_id, source, event_id, raw, at, payload)
        assert raw.reason == 'superseded_observation'
        if at == first:
            assert replayed == old_oid
        assert active_versions() == applied
        assert conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone() == versions

    verified = [*corrected[:2], dict(corrected[2], source_goal_id='late-first'), corrected[3]]
    assert read_goal_snapshot(conn, ended + timedelta(days=1), verified, finished=True) == {'reviewed': 1}
    final = active_versions()
    assert len(final) == 4 and all(row[-1] for row in final)
    continued = next(row for row in final if row[1] == 70)
    assert continued[0] == earlier[0] and continued[2] == 1
    assert continued[3] > earlier[3] and continued[4] == earlier[4] + 1


@DB
def test_shifted_same_minute_goals_without_proven_sequence_stay_pending(conn):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    original = [dict(goal_at(90, 2, score), detail='Гол') for score in (1, 2)]
    assert read_goal_snapshot(conn, first, original) == {'waiting_end': 1}
    known = selected_goals(conn)
    goal_ids = [row[0] for row in known]
    partial = [dict(goal, score_after_home=score) for goal, score in zip(original, (2, 3))]
    observed = first + timedelta(minutes=1)
    for at in (observed, observed + timedelta(minutes=1)):
        assert read_goal_snapshot(conn, at, partial, valid=False,
                                  receipt=observed.isoformat()) == {'waiting_end': 1}
        assert selected_goals(conn) == known

    assert conn.execute("""
        SELECT candidate_goal_ids, reason FROM live_signal_goal_pending ORDER BY ordinal
    """).fetchall() == [(goal_ids, 'identity_ambiguous')] * 2
    assert conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone() == (2,)


@DB
@pytest.mark.parametrize('independent_goal', [False, True])
def test_ambiguous_fonbet_minute_correction_keeps_old_goals_until_resolved(conn, independent_goal):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    original = [
        dict(goal_at(90, 2, 1), detail='Гол', source_goal_id='first'),
        dict(goal_at(90, 2, 2), detail='Гол'),
    ]
    assert read_goal_snapshot(conn, first, original) == {'waiting_end': 1}
    known = selected_goals(conn)
    first_id, second_id = [row[0] for row in known]
    corrected = [dict(goal_at(88, None, 1), detail='Гол'), original[1]]
    if independent_goal:
        corrected.append(dict(goal_at(90, 5, 2), detail='Гол',
                              credited_side='away', score_after_away=1))

    ended = first + timedelta(minutes=1)
    for at in (ended, ended + timedelta(minutes=5)):
        assert read_goal_snapshot(conn, at, corrected, finished=True,
                                  receipt=ended.isoformat()) == {'preliminary': 1}
        assert conn.execute("""
            SELECT goal_id, minute, added_minute, score_after_home FROM live_signal_goals
            WHERE active AND credited_side='home' ORDER BY score_after_home
        """).fetchall() == known
        assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone() == (
            3 if independent_goal else 2,
        )
        assert conn.execute("""
            SELECT timeline_complete,incomplete_reason FROM live_signal_tracking
        """).fetchone() == (False, 'identity_ambiguous')

    assert conn.execute("""
        SELECT payload->>'minute',candidate_goal_ids,reason FROM live_signal_goal_pending ORDER BY ordinal
    """).fetchall() == [
        ('88', [first_id, second_id], 'identity_ambiguous'),
        ('90', [first_id, second_id], 'identity_ambiguous'),
    ]

    # An authoritative link to the original goal resolves the remaining pair too.
    verified = [dict(corrected[0], source_goal_id='first'), *corrected[1:]]
    assert read_goal_snapshot(conn, ended + timedelta(days=1), verified, finished=True) == {'reviewed': 1}
    rows = conn.execute("""
        SELECT goal_id, minute, added_minute, score_after_home FROM live_signal_goals
        WHERE active AND credited_side='home' ORDER BY score_after_home
    """).fetchall()
    assert rows == [(first_id, 88, None, 1), known[1]]
    assert conn.execute('SELECT count(*) FROM live_signal_goals').fetchone() == (
        3 if independent_goal else 2,
    )
    assert conn.execute("""
        SELECT payload->>'minute' FROM live_signal_goal_versions WHERE goal_id=%s ORDER BY observation_id
    """, (first_id,)).fetchall() == [('90',), ('88',)]


@DB
@pytest.mark.parametrize('verified', [True, False])
def test_disputed_late_goals_do_not_absorb_an_earlier_same_clock_match(conn, verified):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    original = [
        dict(goal_at(70, None, 1), detail='Гол'),
        dict(goal_at(90, 2, 2), detail='Гол'),
        dict(goal_at(90, 2, 3), detail='Гол'),
    ]
    assert read_goal_snapshot(conn, first, original) == {'waiting_end': 1}
    earlier_id, *late_ids = [goal[0] for goal in selected_goals(conn)]
    corrected = [
        dict(goal_at(69, None, 0), detail='Гол', credited_side='away', score_after_away=1),
        dict(original[0], score_after_away=1),
        dict(original[1], minute=88, added_minute=None, score_after_away=1),
        dict(original[2], score_after_away=1),
    ]
    observed = first + timedelta(minutes=1)
    repeat_at = observed + timedelta(minutes=5 if verified else 1)
    for at in (observed, repeat_at):
        assert read_goal_snapshot(conn, at, corrected, finished=verified, valid=verified,
                                  receipt=observed.isoformat()) == {
            'preliminary' if verified else 'waiting_end': 1,
        }

    assert conn.execute("""
        SELECT goal_id,score_after_home,score_after_away FROM live_signal_goals WHERE active AND minute=70
    """).fetchall() == [(earlier_id, 1, int(verified))]
    pending = dict(conn.execute("""
        SELECT (payload->>'minute')::int,candidate_goal_ids FROM live_signal_goal_pending
    """).fetchall())
    assert pending[88] == pending[90] == late_ids
    assert conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone() == (2 if verified else 3,)
    if verified:
        assert 70 not in pending
        assert conn.execute("""
            SELECT credited_side,score_after_home,score_after_away FROM live_signal_goals WHERE active AND minute=69
        """).fetchall() == [('away', 0, 1)]
        assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone() == (4,)
    else:
        assert earlier_id in pending[70]
    versions = conn.execute("""
        SELECT payload->>'score_after_away' FROM live_signal_goal_versions
        WHERE goal_id=%s ORDER BY observation_id
    """, (earlier_id,)).fetchall()
    assert versions == ([('0',), ('1',)] if verified else [('0',)])


@DB
@pytest.mark.parametrize('repeat_receipt', [True, False])
def test_known_penalty_is_updated_while_neighbor_type_conflict_is_resolved(conn, repeat_receipt):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    penalty = dict(goal_at(90, 2, 1), detail='Пенальти забит')
    open_play = dict(goal_at(90, 2, 2), detail='Гол', source_goal_id='second')
    assert read_goal_snapshot(conn, first, [penalty, open_play]) == {'waiting_end': 1}
    penalty_id, open_play_id = [goal[0] for goal in selected_goals(conn)]
    corrected = [
        dict(goal_at(88, None, 0), detail='Гол', credited_side='away', score_after_away=1),
        dict(penalty, score_after_away=1),
        dict(goal_at(90, 2, 2), detail='Автогол', score_after_away=1),
    ]
    ended = first + timedelta(minutes=1)
    for index, at in enumerate((ended, ended + timedelta(minutes=5))):
        assert read_goal_snapshot(conn, at, corrected, finished=True,
                                  receipt=(ended if repeat_receipt else at).isoformat()) == {'preliminary': 1}
        assert conn.execute("""
            SELECT goal_id,score_after_home,score_after_away FROM live_signal_goals
            WHERE active AND source_detail='Пенальти забит'
        """).fetchone() == (penalty_id, 1, 1)
        assert conn.execute("""
            SELECT candidate_goal_ids,payload->>'detail' FROM live_signal_goal_pending
        """).fetchall() == [([open_play_id], 'Автогол')] * (1 if repeat_receipt else index + 1)
        assert conn.execute("""
            SELECT source_detail,active FROM live_signal_goals WHERE goal_id=%s
        """, (open_play_id,)).fetchone() == ('Гол', True)
        assert conn.execute('SELECT count(*) FROM live_signal_goals WHERE active').fetchone() == (3,)
        assert conn.execute("""
            SELECT count(*) FROM live_signal_goals WHERE active AND credited_side='away' AND minute=88
        """).fetchone() == (1,)

    verified = [*corrected[:2], dict(corrected[2], source_goal_id='second')]
    result = read_goal_snapshot(conn, ended + timedelta(days=1), verified, finished=True)
    assert result == {'reviewed': 1}, conn.execute('SELECT incomplete_reason FROM live_signal_tracking').fetchone()
    assert conn.execute("""
        SELECT goal_id,source_detail,score_after_away FROM live_signal_goals
        WHERE active AND credited_side='home' ORDER BY score_after_home
    """).fetchall() == [(penalty_id, 'Пенальти забит', 1), (open_play_id, 'Автогол', 1)]
    assert conn.execute('SELECT count(*) FROM live_signal_goals').fetchone() == (3,)


@DB
@pytest.mark.parametrize('structure_valid', [False, True])
def test_incomplete_type_conflict_keeps_penalty_id_until_final_result(conn, structure_valid):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    penalty = dict(goal_at(90, 2, 1), detail='Пенальти забит')
    assert read_goal_snapshot(conn, first, [penalty]) == {'waiting_end': 1}

    def goal_state():
        return conn.execute("""
            SELECT goal_id,source_detail,score_after_home,active,observation_id,revision
            FROM live_signal_goals ORDER BY score_after_home
        """).fetchall()

    original = goal_state()
    penalty_id = original[0][0]
    open_play = dict(penalty, detail='Гол')
    at = first + timedelta(minutes=1)
    assert read_goal_snapshot(conn, at, [open_play], valid=structure_valid) == {'waiting_end': 1}
    assert goal_state() == original
    assert conn.execute("""
        SELECT candidate_goal_ids,payload->>'detail',reason FROM live_signal_goal_pending
    """).fetchall() == [([penalty_id], 'Гол', 'identity_ambiguous')]
    assert conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone() == (1,)

    final = [open_play, dict(penalty, score_after_home=2)]
    ended = at + timedelta(minutes=1)
    assert read_goal_snapshot(conn, ended, final, finished=True) == {'preliminary': 1}
    resolved = goal_state()
    assert [row[1:4] for row in resolved] == [('Гол', 1, True), ('Пенальти забит', 2, True)]
    assert resolved[0][0] != penalty_id and resolved[1][0] == penalty_id
    assert read_goal_snapshot(conn, ended + timedelta(days=1), final, finished=True,
                              receipt=ended.isoformat()) == {'reviewed': 1}
    assert goal_state() == resolved
    assert conn.execute('SELECT count(*) FROM live_signal_goal_versions').fetchone() == (3,)


@DB
def test_incomplete_type_conflict_within_crowded_minute_preserves_candidate(conn):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    penalty = dict(goal_at(90, 2, 1), detail='Пенальти забит')
    open_play = dict(goal_at(90, 2, 2), detail='Гол')
    assert read_goal_snapshot(conn, first, [penalty, open_play]) == {'waiting_end': 1}
    known = selected_goals(conn)
    assert read_goal_snapshot(conn, first + timedelta(minutes=1), [dict(penalty, detail='Автогол')],
                              valid=False) == {'waiting_end': 1}
    assert selected_goals(conn) == known
    assert conn.execute("""
        SELECT candidate_goal_ids,payload->>'detail' FROM live_signal_goal_pending
    """).fetchall() == [([known[0][0]], 'Автогол')]


@DB
@pytest.mark.parametrize('detail,provider_id,expected', [
    (None, None, 'Пенальти забит'),
    ('Гол', 'same-goal', 'Гол'),
])
def test_incomplete_goal_type_uses_known_type_or_confirmed_source_id(conn, detail, provider_id, expected):
    seed(conn)
    first = NOW + timedelta(minutes=20)
    penalty = dict(goal_at(90, 2, 1), detail='Пенальти забит', source_goal_id=provider_id)
    assert read_goal_snapshot(conn, first, [penalty]) == {'waiting_end': 1}
    goal_id, = conn.execute('SELECT goal_id FROM live_signal_goals').fetchone()
    assert read_goal_snapshot(conn, first + timedelta(minutes=1), [dict(penalty, detail=detail)],
                              valid=False) == {'waiting_end': 1}
    assert conn.execute('SELECT goal_id,source_detail,active FROM live_signal_goals').fetchall() == [
        (goal_id, expected, True),
    ]
    assert conn.execute('SELECT count(*) FROM live_signal_goal_pending').fetchone() == (0,)


@DB
@pytest.mark.parametrize('terminal_source,status,live_source,offset', [
    ('browser', 'FT', 'browser', -30),
    ('browser', 'ABD', 'browser', -30),
    ('collector', 'FT', 'browser', -20),
    ('browser', 'FT', 'collector', -30),
    ('browser', 'FT', 'browser', 0),
])
def test_fonbet_discards_stale_terminal_then_accepts_fresh_end(conn, terminal_source, status, live_source, offset):
    seed(conn)
    conn.execute('INSERT INTO live_signal_reminders(signal_id) VALUES(1)')
    at = NOW + timedelta(minutes=10)

    def live(*args, **kwargs):
        # Confirm the clock without requiring any goals or score fields.
        return Timeline(status='2H', elapsed=89, period='regulation_2h'), at.isoformat(), {}

    if live_source == 'collector':
        live_context(conn, '2H', 89, at)
        rejected_at = at + timedelta(seconds=5)
    else:
        assert run_results(conn, now_fn=lambda: at, fonbet_loader=live) == {'waiting_end': 1}
        rejected_at = at + timedelta(minutes=1)
    stale_at = at + timedelta(seconds=offset)
    if terminal_source == 'collector':
        live_context(conn, status, 90, stale_at)
        rejected_loader = live
    else:
        rejected_loader = lambda *a, **k: (Timeline(status=status), stale_at.isoformat(), {})

    assert run_results(conn, now_fn=lambda: rejected_at, fonbet_loader=rejected_loader) == {'waiting_end': 1}
    assert conn.execute("""
        SELECT t.status,t.regulation_end_confirmed_at,t.review_due_at,r.state
        FROM live_signal_tracking t JOIN live_signal_reminders r USING (signal_id)
    """).fetchone() == ('waiting_end', None, None, 'waiting')
    if terminal_source == 'browser':
        assert conn.execute("""
            SELECT incomplete_reason FROM live_signal_goal_observations
            WHERE observed_at=%s AND payload->'timeline'->>'status'=%s
        """, (stale_at, status)).fetchone() == ('superseded_observation',)

    finished_at = at + timedelta(minutes=2)
    result = lambda *a, **k: (Timeline(status='FT', complete=True, regulation_score=(0, 0)),
                             finished_at.isoformat(), {})
    assert run_results(conn, now_fn=lambda: finished_at, fonbet_loader=result) == {'preliminary': 1}
    assert run_results(conn, now_fn=lambda: finished_at, fonbet_loader=result) == {}
    assert conn.execute("""
        SELECT t.status,t.regulation_end_confirmed_at,t.review_due_at,r.state,r.reason
        FROM live_signal_tracking t JOIN live_signal_reminders r USING (signal_id)
    """).fetchone() == ('preliminary', finished_at, finished_at + timedelta(days=1),
                        'cancelled', 'regulation_end_confirmed')


@DB
def test_later_post_match_projection_survives_delayed_void(conn):
    seed(conn)
    ended = NOW + timedelta(minutes=10)
    first_goal = goal_at(87, None, 1)
    assert run_results(conn, now_fn=lambda: ended, fonbet_loader=lambda *a, **k: (
        Timeline(goals=[first_goal], status='FT'), ended.isoformat(), {},
    )) == {'preliminary': 1}
    corrected = [first_goal, goal_at(90, 3, 2)]
    assert read_goal_snapshot(conn, ended + timedelta(minutes=5), corrected,
                              finished=True) == {'preliminary': 1}
    goals = selected_goals(conn)
    review_at = ended + timedelta(days=1)
    delayed = lambda *a, **k: (Timeline(status='ABD'), (ended + timedelta(minutes=2)).isoformat(), {})
    assert run_results(conn, now_fn=lambda: review_at, fonbet_loader=delayed) == {'review_due': 1}
    assert selected_goals(conn) == goals
    assert conn.execute("""
        SELECT timeline_complete,regulation_end_confirmed_at,review_due_at FROM live_signal_tracking
    """).fetchone() == (True, ended, review_at)
    assert read_goal_snapshot(conn, review_at + timedelta(days=1), corrected, finished=True) == {'reviewed': 1}
