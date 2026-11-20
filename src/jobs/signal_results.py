"""Bounded live and post-match goal observations for Fonbet-first signals."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
import time
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from src.jobs.alert_dispatch import rows
from src.jobs.signal_goal_store import latest_fonbet_browser_clock, relate_goals, save_timeline
from src.jobs.signal_goals import load_fonbet_timeline


ENDED = {'REG_END', 'FT', 'ET', 'BT', 'P', 'AET', 'PEN'}
VOID = {'CANC', 'ABD', 'AWD', 'WO'}
ACTIVE = "t.first_source='fonbet' AND t.goals_enabled AND t.status IN ('waiting_end','preliminary','review_due')"
MINUTE = timedelta(minutes=1)
ORDINARY = timedelta(minutes=5)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


def end_state(status):
    return 'void' if status in VOID else 'ended' if status in ENDED else 'waiting'


def schedule_after(signal, now, complete):
    end = signal['regulation_end_confirmed_at']
    review = signal.get('review_due_at') or end + DAY
    day = signal['review_day']
    if now < review and day == 0:
        if not complete and now < end + HOUR:
            delay = (5,15,30)[min(signal['primary_attempts'],2)]
            due = min(now+timedelta(minutes=delay),end+HOUR)
        else:
            due = review
        return 'preliminary', 0, due
    day += 1
    if complete:return 'reviewed',day,None
    if day >= 8:return 'incomplete_final',8,None
    return 'review_due',day,now+DAY


def phase_for(signal, context):
    if context.get('status') == 'absent' and (
        not signal['last_poll_at'] or context['observed_at'] > signal['last_poll_at']
    ):
        return 'unavailable'
    if (context.get('present') and context.get('status') == '2H'
            and context.get('period') == 'regulation_2h' and (context.get('elapsed') or 0) >= 89
            and (not signal['last_poll_at'] or context['observed_at'] > signal['last_poll_at']
                 or signal['live_phase']=='late')):
        return 'late'
    return signal['live_phase'] or 'ordinary'


def terminal_conflicts_with_live(conn, signal, observed_at, now):
    if observed_at > now:
        return True
    source_event_id = signal['first_source_event_id']
    browser_clock = latest_fonbet_browser_clock(conn, signal['alert_match_id'], source_event_id, now)
    if browser_clock is not None and observed_at <= browser_clock[0]:
        return True
    return conn.execute("""
        SELECT EXISTS (
            SELECT 1 FROM live_alert_context
            WHERE source='fonbet' AND source_event_id=%s AND present AND poll_id IS NOT NULL
              AND status='2H' AND period='regulation_2h'
              AND requested_at>%s AND requested_at<=observed_at
              AND observed_at BETWEEN %s AND %s
        )
    """, (source_event_id, signal['confirmed_at'], observed_at, now)).fetchone()[0]


def queue_due(conn, now, scheduled):
    slot = now.replace(second=0, microsecond=0)
    ordinary_tick = not scheduled or slot.minute % 5 == 0
    jobs = []
    candidates = rows(conn, f"""SELECT t.*,c.status AS context_status,c.elapsed AS context_elapsed,
      c.period AS context_period,c.present AS context_present,c.poll_id AS context_poll_id,
      c.observed_at AS context_observed_at
      FROM live_signal_tracking t LEFT JOIN live_alert_context c ON c.source='fonbet'
        AND c.source_event_id=t.first_source_event_id AND t.regulation_end_confirmed_at IS NULL
        AND c.observed_at BETWEEN %s AND %s AND c.poll_id IS NOT NULL
        AND c.requested_at>t.confirmed_at AND c.requested_at<=c.observed_at
      WHERE {ACTIVE} AND (t.lease_until IS NULL OR t.lease_until<=%s)
        AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=%s OR c.status IS NOT NULL)""",
      (now-timedelta(minutes=2),now,now,now))
    for signal in candidates:
        live = signal['regulation_end_confirmed_at'] is None
        context = {key:signal['context_'+key] for key in ('status','elapsed','period','present','poll_id','observed_at')}
        if end_state(context['status']) != 'waiting' and terminal_conflicts_with_live(
                conn, signal, context['observed_at'], now):
            context = {}
        phase = phase_for(signal, context) if live else None
        late = phase == 'late'
        state = end_state(context.get('status'))
        if not ordinary_tick and not late and not (live and signal['live_phase']=='late' and state!='waiting'):
            continue
        if live and state == 'waiting' and signal['last_live_slot'] is not None and signal['last_live_slot'] >= slot:
            continue
        due = signal['next_attempt_at'] or signal['confirmed_at']
        wake = live and (state != 'waiting' or late and signal['live_phase'] != 'late')
        if due <= now or wake:
            jobs.append((signal, context, phase, due))
    def priority(job):
        signal,_,phase,admission = job
        due = signal['result_due_at'] or admission
        if phase is None and due+ORDINARY<=now:
            return 0,due,due,signal['signal_id']
        return (1 if phase=='late' else 2,
                signal['last_poll_at'] or signal['confirmed_at'],admission,signal['signal_id'])

    return sorted(jobs,key=priority)


def cancel_reminder(conn, signal_id, now, reason):
    conn.execute("""UPDATE live_signal_reminders SET state='cancelled',reason=%s,updated_at=%s
      WHERE signal_id=%s AND state IN ('waiting','retry')""", (reason,now,signal_id))


def confirm_end(conn, signal, now, status, evidence, observed_at):
    signal['regulation_end_confirmed_at'] = now
    signal['review_due_at'] = now + DAY
    conn.execute("""UPDATE live_signal_tracking SET regulation_end_confirmed_at=%s,end_context=%s,
      primary_due_at=%s,review_due_at=%s,result_due_at=%s,result_due_from_admission=false,
      live_phase=NULL WHERE signal_id=%s""",
      (now,Jsonb({'source':'fonbet','id':signal['first_source_event_id'],'status':status,
                  'observed_at':observed_at.isoformat(),'evidence':evidence}),
       now,signal['review_due_at'],now,signal['signal_id']))
    cancel_reminder(conn, signal['signal_id'], now, 'regulation_end_confirmed')


def void_signal(conn, signal, now, status, evidence, *, attempted):
    signal_id = signal['signal_id']
    context = Jsonb({'source':'fonbet','id':signal['first_source_event_id'],
                     'status':status,'evidence':evidence})
    conn.execute("""UPDATE live_signal_tracking SET status='void',lease_until=NULL,next_attempt_at=NULL,
      live_phase=NULL,result_due_at=NULL,result_due_from_admission=false,
      incomplete_reason='match_void',end_context=%s,
      last_poll_at=CASE WHEN %s THEN %s ELSE last_poll_at END,
      last_attempt_at=CASE WHEN %s THEN %s ELSE last_attempt_at END WHERE signal_id=%s""",
      (context,attempted,now,attempted,now,signal_id))
    log_attempt(conn,signal_id,now,'void','match_void',status=status,contacted=attempted)
    cancel_reminder(conn, signal_id, now, 'match_void')


def poll_fonbet(conn, signal, loader):
    eid = signal['first_source_event_id']
    row = conn.execute("""SELECT event_url FROM fonbet_live_match_statistics_75
      WHERE fonbet_event_id=%s""", (eid,)).fetchone()
    url = row[0] if row else signal['first_context'].get('event_url')
    if not url:
        return None, None, {}, 'event_url_missing'
    from urllib.parse import urlsplit
    if urlsplit(url).path.rstrip('/').split('/')[-1] != eid:
        return None, None, {}, 'event_identity_mismatch'
    timeline, receipt, payload = loader(
        url, regulation_end_confirmed=bool(signal['regulation_end_confirmed_at']),
        result_day=signal['first_sent_at'].astimezone(ZoneInfo('Europe/Moscow')).date().isoformat(),
    )
    return timeline, receipt, payload, None if timeline.attempted else timeline.reason or 'contact_unconfirmed'


def observe_time(receipt, now):
    if isinstance(receipt, str):
        try:
            observed = datetime.fromisoformat(receipt.replace('Z','+00:00'))
            if observed.tzinfo is not None:
                return observed
        except ValueError:
            pass
    return now


def log_attempt(conn, signal_id, now, stage, reason, **details):
    conn.execute("""INSERT INTO live_signal_check_attempts(signal_id,attempted_at,stage,reason,details)
      VALUES(%s,%s,%s,%s,%s)""", (signal_id,now,stage,reason,Jsonb(details)))


def compare_last_live(conn, signal, timeline, payload):
    evidence = payload.get('completion_evidence') or {}
    score = evidence.get('regulation_score')
    oid = signal['last_live_observation_id']
    if score is None or not oid or payload.get('parsed_regulation_score') is not None:
        return
    archived = conn.execute('''SELECT payload->'payload' FROM live_signal_goal_observations
      WHERE observation_id=%s AND source='fonbet' AND source_event_id=%s''',
      (oid,signal['first_source_event_id'])).fetchone()
    if not archived:
        return
    live = archived[0]
    payload['compared_live_observation_id'] = oid
    names = lambda pair: tuple(name.casefold() for name in pair if isinstance(name,str))
    if live.get('teams') and names(live['teams']) != names(evidence['teams']):
        timeline.reason = 'result_sides_mismatch'
    elif live.get('parsed_regulation_score') is not None and live['parsed_regulation_score'] != score:
        timeline.reason = 'regulation_score_mismatch'


def finish_attempt(conn, signal, phase, slot, now, timeline, receipt, payload, deferred):
    gid = signal['signal_id']
    end = signal['regulation_end_confirmed_at']
    observed_at = observe_time(receipt, now)
    state = end_state(timeline.status) if timeline and timeline.attempted and not deferred else 'waiting'
    superseded = state != 'waiting' and terminal_conflicts_with_live(conn, signal, observed_at, now)
    if state == 'void' and end is not None:
        end_context = signal['end_context'] or {}
        end_at = observe_time(end_context.get('observed_at') or
                              (end_context.get('evidence') or {}).get('observed_at'), end)
        superseded = superseded or observed_at <= end_at
    observation = None
    if timeline and timeline.attempted:
        if (end or state == 'ended') and state != 'void' and not superseded:
            compare_last_live(conn,signal,timeline,payload)
        observation = save_timeline(conn, signal['alert_match_id'], 'fonbet', signal['first_source_event_id'],
                                    timeline, observed_at, payload, superseded=superseded, status_only=state == 'void')
    superseded = timeline is not None and timeline.reason == 'superseded_observation'
    if state=='void' and not superseded:
        void_signal(conn,signal,now,timeline.status,payload,attempted=timeline.attempted)
        return 'void'
    if not end and state=='ended' and not superseded:
        confirm_end(conn,signal,now,timeline.status,payload.get('completion_evidence') or {
            'clock_text':payload.get('clock_text'),'period_text':payload.get('period_text')}, observed_at)
        end = now
    if end is None and timeline:
        if timeline.status=='2H' and timeline.period=='regulation_2h' and (timeline.elapsed or 0)>=89:
            phase='late'
        elif timeline.reason=='live_event_missing':
            phase='unavailable'
    live = end is None
    frequency = MINUTE if live and phase=='late' else ORDINARY
    next_due = max(slot + frequency, now.replace(second=0,microsecond=0)+MINUTE)
    if deferred:
        log_attempt(conn,gid,now,'deferred_internal',deferred,live_phase=phase,slot=slot.isoformat(),error=payload.get('error'))
        original_due = signal['result_due_at'] if not live else None
        admission_fallback = not live and original_due is None
        conn.execute("""UPDATE live_signal_tracking SET lease_until=NULL,deferred_reason=%s,
          live_phase=%s,next_attempt_at=%s,missed_live_slots=missed_live_slots+%s,
          result_due_at=%s,result_due_from_admission=%s WHERE signal_id=%s""",
          (deferred,phase if live else None,next_due,int(live and phase=='late'),
           (signal['next_attempt_at'] or end) if admission_fallback else original_due,
           admission_fallback or signal['result_due_from_admission'],gid))
        return 'deferred_internal'

    complete = timeline.complete
    unknown = signal['order_unknown_count']
    if not superseded and (complete or timeline.goals):
        unknown = relate_goals(conn, signal, 'fonbet')
    if live:
        status, day = 'waiting_end', 0
        stage = 'live_late' if phase=='late' else 'waiting_end'
    else:
        status, day, next_due = schedule_after(signal,now,complete)
        stage = 'daily_review' if day else 'primary'
    log_attempt(conn,gid,now,stage,timeline.reason,complete=complete,review_day=day,
                outcome=timeline.outcome,observation_id=observation,
                slot=slot.isoformat() if live else None, elapsed=timeline.elapsed,added_minute=timeline.added_minute)
    live_observation = observation if (
        live and not superseded
        and (timeline.goals or payload.get('score') is not None)
    ) else None
    conn.execute("""UPDATE live_signal_tracking SET status=%s,review_day=%s,next_attempt_at=%s,
      last_attempt_at=%s,lease_until=NULL,deferred_reason=NULL,last_poll_at=%s,
      timeline_complete=%s,order_unknown_count=%s,incomplete_reason=%s,last_error=NULL,
      primary_attempts=primary_attempts+%s,live_phase=%s,
      result_due_at=%s,result_due_from_admission=false,
      last_live_slot=CASE WHEN %s THEN %s ELSE last_live_slot END,
      last_live_observation_id=COALESCE(%s,last_live_observation_id) WHERE signal_id=%s""",
      (status,day,next_due,now,now,signal['timeline_complete'] if superseded else complete and not live,
       unknown,timeline.reason,
       int(not live and day==0),phase if live else None,next_due if not live else None,
       live and phase=='late',slot,live_observation,gid))
    return status


def run_results(conn, *, now_fn=lambda:datetime.now(timezone.utc), fonbet_loader=load_fonbet_timeline,
                limit=10, browser_limit=1, budget_seconds=100, scheduled=False):
    enabled, version = conn.execute('SELECT tracking_enabled,version FROM live_signal_config WHERE singleton').fetchone()
    if not enabled or version<2:return {'disabled':1}
    if not conn.execute('SELECT pg_try_advisory_lock(33001)').fetchone()[0]:return {'busy':1}
    report = Counter()
    deadline = time.monotonic() + budget_seconds
    browsers = 0
    try:
        now = now_fn()
        slot = now.replace(second=0,microsecond=0)
        with conn.transaction():
            conn.execute(f"""UPDATE live_signal_tracking t SET live_phase='unavailable',
              incomplete_reason='live_event_missing'
              FROM live_alert_context c
              WHERE {ACTIVE} AND t.regulation_end_confirmed_at IS NULL AND t.live_phase='late'
                AND c.source='fonbet' AND c.source_event_id=t.first_source_event_id
                AND c.status='absent' AND c.poll_id IS NOT NULL
                AND c.requested_at>t.confirmed_at AND c.requested_at<=c.observed_at
                AND c.observed_at BETWEEN %s AND %s
                AND (t.last_poll_at IS NULL OR c.observed_at>t.last_poll_at)""",
                (now-2*MINUTE,now))
        for signal, context, phase, due in queue_due(conn,now,scheduled)[:limit]:
            gid = signal['signal_id']
            skipped = 0
            if phase=='late' and signal['last_live_slot'] is not None:
                scheduled_slot = due.replace(second=0, microsecond=0)
                skipped = max(0, (slot-scheduled_slot)//MINUTE)
            with conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock(27001)')
                claimed = conn.execute(f"""UPDATE live_signal_tracking t SET lease_until=%s,
                  last_live_slot=CASE WHEN %s THEN %s ELSE last_live_slot END,live_phase=%s,
                  missed_live_slots=missed_live_slots+%s
                  WHERE t.signal_id=%s AND {ACTIVE} RETURNING signal_id""",
                  (now+timedelta(minutes=3),phase=='late',slot,phase,skipped,gid)).fetchone()
                if not claimed:
                    continue
                if skipped:
                    log_attempt(conn,gid,now,'live_gap','missed_cycles',slots=skipped)
                state = end_state(context.get('status'))
                if state in ('void','ended') and not terminal_conflicts_with_live(
                        conn, signal, context['observed_at'], now):
                    evidence={'poll_id':context['poll_id'],'observed_at':context['observed_at'].isoformat()}
                    if state=='void':
                        void_signal(conn,signal,now,context['status'],evidence,attempted=False)
                        report['void']+=1
                        continue
                    confirm_end(conn,signal,now,context['status'],evidence,context['observed_at'])
            timeline = receipt = deferred = None
            payload = {}
            if browsers>=browser_limit:
                deferred='browser_budget'
            elif deadline-time.monotonic()<65:
                deferred='time_budget'
            else:
                browsers+=1
                try:
                    timeline,receipt,payload,deferred = poll_fonbet(conn,signal,fonbet_loader)
                except (OSError, ValueError, TimeoutError) as exc:
                    deferred='contact_unconfirmed'
                    payload={'error':type(exc).__name__}
            with conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock(27001)')
                current = rows(conn, 'SELECT * FROM live_signal_tracking WHERE signal_id=%s FOR UPDATE', (gid,))[0]
                if current['status']=='void':
                    conn.execute('UPDATE live_signal_tracking SET lease_until=NULL WHERE signal_id=%s', (gid,))
                    continue
                outcome = finish_attempt(conn,current,phase,slot,now_fn(),timeline,receipt,payload,deferred)
                report[outcome]+=1
        return dict(report)
    finally:
        conn.execute('SELECT pg_advisory_unlock(33001)')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit',type=int,default=10)
    parser.add_argument('--browser-limit',type=int,default=1)
    parser.add_argument('--scheduled',action='store_true',help='Run ordinary stages only on the existing five-minute cadence')
    args=parser.parse_args(argv)
    url=os.environ.get('DATABASE_URL')
    if not url:parser.error('DATABASE_URL is required')
    try:
        with psycopg.connect(url,autocommit=True) as conn:
            report=run_results(conn,limit=args.limit,browser_limit=args.browser_limit,scheduled=args.scheduled)
        print(json.dumps(report))
        return 0
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
