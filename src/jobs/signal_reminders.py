"""One reminder per canonical signal, confirmed by fresh same-source statistics."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import time

from psycopg.types.json import Jsonb

from src.jobs.alert_rules import TTL, fresh, pressure, usable
from src.jobs.signal_goal_store import latest_fonbet_browser_clock
from src.jobs.signal_reminder_history import available_polls, current_api_team_ids, expected_api_teams


TERMINAL = {'REG_END', 'FT', 'AET', 'PEN', 'ET', 'BT', 'P', 'CANC', 'ABD', 'AWD', 'WO'}
WAIT_LIMIT = timedelta(minutes=20)


def same_poll(job, context, snapshot):
    collection = context.get('collection_run_id')
    return snapshot['statistics_poll_id'] == context['poll_id'] or (
        job['first_source']=='api_football' and collection is not None
        and snapshot.get('statistics_collection_run_id') == collection
        and context.get('api_team_ids') is not None
        and snapshot.get('api_team_ids') == context['api_team_ids']
    )


def decision(job, contexts, snapshot, now):
    confirmed_at = job['confirmed_at']
    current = [
        c for c in contexts if c['poll_id'] and fresh(c['completed_at'], now)
        and c['requested_at'] is not None and confirmed_at < c['requested_at'] <= c['completed_at']
    ]
    if any(c['status'] in TERMINAL for c in current):
        return 'regulation_end_or_void', None
    if any(c['status'] == '2H' and (c['elapsed'] or 0) > 75 for c in current):
        return 'outside_window', None
    if now >= confirmed_at + WAIT_LIMIT:
        return 'window_expired', None
    context = next((c for c in current if c['source'] == job['first_source']
                    and c['source_event_id'] == job['first_source_event_id']), None)
    if (not context or not context['present'] or context['status']!='2H'
            or context['period']!='regulation_2h' or not 65 <= (context['elapsed'] or 0) <= 75
            or not snapshot or not usable(snapshot, now)):
        return 'waiting', None
    if not same_poll(job,context,snapshot):
        return 'waiting', None
    if not (confirmed_at < snapshot['statistics_context_at'] <= snapshot['observed_at']
            and confirmed_at < snapshot['statistics_requested_at'] <= snapshot['observed_at']):
        return 'waiting', None
    verdict = 'ready' if pressure(snapshot['home_shots'],snapshot['away_shots']) else 'trigger_cleared'
    return verdict, dict(context, home_shots=snapshot['home_shots'], away_shots=snapshot['away_shots'],
                         statistics_observed_at=snapshot['observed_at'])


def data(conn, now, match_ids):
    from src.jobs.alert_dispatch import SELECT_SLICES, rows
    contexts = {}
    past_window = {}
    for context in rows(conn, """SELECT a.alert_match_id,a.source,a.source_event_id,
        c.observed_at,c.status,c.elapsed,c.present,c.format_tags,c.poll_id,
        c.requested_at,c.completed_at,c.period,c.added_minute,o.run_id AS collection_run_id,
        o.payload AS api_payload,o.requested_at AS api_requested_at,o.observed_at AS api_observed_at
      FROM live_alert_aliases a LEFT JOIN live_alert_context c USING(source,source_event_id)
      LEFT JOIN api_observations o ON o.observation_id=CASE
        WHEN c.source='api_football' AND c.poll_id ~ '^[0-9]{1,18}$' THEN c.poll_id::bigint END
        AND o.endpoint='/fixtures' AND o.params->>'live'='all'
      WHERE a.alert_match_id=ANY(%s)""", (match_ids,)):
        payload = context.pop('api_payload')
        requested_at = context.pop('api_requested_at')
        observed_at = context.pop('api_observed_at')
        if context['source'] == 'api_football':
            team_ids = current_api_team_ids(context, payload, requested_at, observed_at)
            if team_ids is None:
                continue
            context['api_team_ids'] = team_ids
        match_id = context['alert_match_id']
        if context['source'] == 'fonbet':
            browser = latest_fonbet_browser_clock(conn, match_id, context['source_event_id'], now)
            if browser is not None:
                browser_at, elapsed = browser
                regular_at = context['observed_at']
                corrected = (context['present'] and context['status'] == '2H'
                             and context['period'] == 'regulation_2h' and context['elapsed'] is not None
                             and context['poll_id'] and context['requested_at'] is not None
                             and context['requested_at'] <= regular_at <= now and regular_at > browser_at)
                if elapsed is not None and elapsed > 75 and not corrected:
                    past_window[match_id] = max(browser_at, past_window.get(match_id, browser_at))
                if context['status'] in TERMINAL and regular_at <= browser_at:
                    continue
        if context['observed_at'] is not None:
            contexts.setdefault(match_id, []).append(context)
    slices = rows(conn, f"SELECT * FROM ({SELECT_SLICES}) slices WHERE source='fonbet'", (now-TTL, now)*2)
    return contexts, {(s['source'], s['event_id']): s for s in slices}, past_window


def dispatch_reminders(conn,send,*,now_fn=lambda:datetime.now(timezone.utc),limit=30,deadline=None,clock=time.monotonic):
    from src.jobs.alert_dispatch import rows,DeliveryFailure,json_ready
    from src.jobs.signal_tracking import DeliveryReceipt
    report=Counter()
    deadline=clock()+85 if deadline is None else deadline
    if conn.execute("SELECT to_regclass('live_signal_config')").fetchone()[0] is None:return {}
    config=conn.execute('SELECT reminders_enabled,version FROM live_signal_config WHERE singleton').fetchone()
    if not config[0] or config[1]<2:return {}
    if not conn.execute('SELECT pg_try_advisory_lock(27001)').fetchone()[0]:return {'reminder_busy':1}

    def cancel(signal_id, reason, now):
        conn.execute("""UPDATE live_signal_reminders SET state='cancelled',reason=%s,updated_at=%s
          WHERE signal_id=%s AND state IN ('waiting','retry')""",(reason,now,signal_id))
        report['reminder_cancelled']+=1

    def evaluate(job, contexts, snapshots, past_window, now):
        expected = expected_api_teams(conn, job)
        aliases = [
            context for context in contexts.get(job['alert_match_id'], [])
            if context['source'] != 'api_football'
            or context['api_team_ids'] == expected.get(context['source_event_id'])
        ]
        source_key = job['first_source'],job['first_source_event_id']
        history = available_polls(conn,job,min(now,job['confirmed_at']+WAIT_LIMIT))
        if job['first_source'] == 'api_football':
            teams = expected.get(job['first_source_event_id'])
            history = [poll for poll in history if teams is not None and poll['api_team_ids'] == teams]
        # API history already validates the archival link in the 75-minute
        # snapshot. Only Fonbet uses a retained snapshot directly.
        if job['first_source']=='fonbet':
            projection = snapshots.get(source_key)
            _,projected_context = decision(job,aliases,projection,now)
            if (projected_context and projection['statistics_poll_id']==projected_context['poll_id']
                    and all(row['statistics_observation_id']!=projection['statistics_observation_id'] for row in history)):
                history.append(dict(projection,context_requested_at=projected_context['requested_at']))
        history.sort(key=lambda row: (row['observed_at'],
                     int(row['statistics_observation_id']) if job['first_source']=='api_football'
                     else row['statistics_observation_id']))
        first = history[0] if history and job['first_decision'] is None else None
        origin = next((c for c in aliases if (c['source'],c['source_event_id'])==source_key),None)
        latest = next((row for row in reversed(history) if origin and same_poll(job,origin,row)),None)
        state,context = decision(job,aliases,latest,now)
        window_at = past_window.get(job['alert_match_id'])
        if state in ('waiting', 'ready') and window_at is not None and window_at > job['confirmed_at']:
            return 'outside_window', None, first
        return state,context,first

    try:
        now=now_fn()
        conn.execute("""UPDATE live_signal_reminders SET state='uncertain',reason='sender_interrupted',updated_at=%s
          WHERE state='sending' AND lease_until<=%s""",(now,now))
        pending=rows(conn,"""SELECT t.*,r.next_attempt_at AS reminder_due,r.first_decision FROM live_signal_tracking t
          JOIN live_signal_reminders r USING(signal_id)
          WHERE r.state IN ('waiting','retry') AND t.first_source IS NOT NULL
          ORDER BY r.next_attempt_at,t.first_sent_at,t.signal_id""")
        if not pending:return dict(report)
        contexts,snapshots,past_window=data(conn,now,[job['alert_match_id'] for job in pending])
        queue=[]
        for job in pending:
            state,_,first=evaluate(job,contexts,snapshots,past_window,now)
            if first:
                outcome = pressure(first['home_shots'],first['away_shots'])
                evidence = json_ready(dict(
                    source=job['first_source'],source_event_id=job['first_source_event_id'],
                    poll_id=first['statistics_poll_id'],observation_id=first['statistics_observation_id'],
                    context_requested_at=first['context_requested_at'],
                    statistics_requested_at=first['statistics_requested_at'],
                    observed_at=first['observed_at'],minute=first['elapsed'],triggered=outcome))
                with conn.transaction():
                    conn.execute("""UPDATE live_signal_reminders SET first_decision=%s,updated_at=%s
                      WHERE signal_id=%s AND first_decision IS NULL AND state IN ('waiting','retry')""",
                      (Jsonb(evidence),now,job['signal_id']))
                    if not outcome:
                        cancel(job['signal_id'],'trigger_cleared',now)
                job['first_decision']=evidence
                if not outcome:
                    continue
            if state=='ready':
                if job['first_decision'] and job['reminder_due']<=now:queue.append(job)
            elif state!='waiting':
                cancel(job['signal_id'],state,now)
        for job in queue[:limit]:
            if deadline-clock()<30:
                report['reminder_budget_deferred']+=1;break
            now=now_fn()
            with conn.transaction():
                contexts,snapshots,past_window=data(conn,now,[job['alert_match_id']])
                state,context,_=evaluate(job,contexts,snapshots,past_window,now)
                blocked=conn.execute("""SELECT EXISTS(SELECT 1 FROM live_signal_tracking t
                  JOIN live_signal_reminders r USING(signal_id) WHERE t.alert_match_id=%s
                  AND t.signal_id<>%s AND r.state IN ('sent','uncertain','sending'))""",
                  (job['alert_match_id'],job['signal_id'])).fetchone()[0]
                if blocked:state='canonical_delivery'
                if state=='waiting':continue
                if state!='ready':
                    cancel(job['signal_id'],state,now);continue
                if deadline-clock()<30:
                    report['reminder_budget_deferred']+=1;break
                conn.execute("""UPDATE live_signal_reminders SET state='sending',attempts=attempts+1,
                  lease_until=%s,context=%s,updated_at=%s WHERE signal_id=%s""",
                  (now+timedelta(seconds=90),Jsonb(json_ready(context)),now,job['signal_id']))
            original=job['first_context']
            minute=str(context['elapsed'])
            if context.get('added_minute'):minute+='+'+str(context['added_minute'])
            text=(f"Напоминание о сигнале №{job['signal_id']} (первое сообщение №{job['first_message_id']})\n"
                  f"{original.get('home','?')} — {original.get('away','?')}\nМинута: {minute}\n"
                  f"Удары в створ во 2-м тайме: {context['home_shots']} : {context['away_shots']}")
            if job['first_source']=='fonbet' and original.get('event_url'):text+='\n'+original['event_url']
            try:
                reply=getattr(send,'reply',None)
                result=reply(text,job['first_message_id']) if callable(reply) else send(text,None)
                mid=result.message_id if isinstance(result,DeliveryReceipt) else result
                if type(mid) is not int or mid<=0:raise DeliveryFailure()
            except Exception as exc:
                definite=isinstance(exc,DeliveryFailure) and exc.definite
                state='retry' if definite else 'uncertain'
                conn.execute("""UPDATE live_signal_reminders SET state=%s,reason=%s,next_attempt_at=%s,
                  lease_until=NULL,updated_at=%s WHERE signal_id=%s""",
                  (state,'telegram_rejected' if definite else 'delivery_unknown',now_fn()+timedelta(seconds=15),now_fn(),job['signal_id']))
                report['reminder_'+state]+=1;continue
            conn.execute("""UPDATE live_signal_reminders SET state='sent',sent_at=%s,telegram_message_id=%s,
              lease_until=NULL,reason=NULL,updated_at=%s WHERE signal_id=%s""",(now_fn(),mid,now_fn(),job['signal_id']))
            report['reminder_sent']+=1
        return dict(report)
    finally:conn.execute('SELECT pg_advisory_unlock(27001)')
