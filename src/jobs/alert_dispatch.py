"""DB-only candidate selection, durable cross-source deduplication and Telegram delivery."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import time
from types import SimpleNamespace

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.jobs.alert_rules import choose, pressure, shots, fresh
from src.jobs.signal_tracking import DeliveryReceipt, enrollment, record_confirmed

LOCK = 27001
SELECT_SLICES = """
SELECT 'api_football' AS source,a.fixture_id::text AS event_id,a.fixture_id,
 NULL::text AS fonbet_event_id,COALESCE(l.name,raw.payload->'league'->>'name','') AS league,
 COALESCE(h.name,raw.payload->'teams'->'home'->>'name','?') AS home,
 COALESCE(v.name,raw.payload->'teams'->'away'->>'name','?') AS away,
 a.elapsed,a.observed_at,a.goals_home,a.goals_away,
 a.statistics->'home'->'2h'->'shots_on_goal' AS home_shots,
 a.statistics->'away'->'2h'->'shots_on_goal' AS away_shots,
 a.kickoff_at,a.league_id,'api_only'::text AS mapping_status,'[]'::jsonb AS candidates,
 c.observed_at AS context_at,c.status,c.elapsed AS current_minute,c.present,c.format_tags,
 'https://www.api-football.com/'::text AS event_url,
 a.requested_at AS statistics_requested_at,a.context_observed_at AS statistics_context_at,
 a.context_observation_id::text AS statistics_poll_id,a.run_id AS statistics_collection_run_id, a.observation_id::text AS statistics_observation_id
FROM live_match_statistics_75 a
LEFT JOIN leagues l USING(league_id)
LEFT JOIN teams h ON h.team_id=a.home_team_id
LEFT JOIN teams v ON v.team_id=a.away_team_id
LEFT JOIN LATERAL (
 SELECT item AS payload FROM api_observations o CROSS JOIN LATERAL jsonb_array_elements(o.payload->'response') item
 WHERE o.observation_id=a.context_observation_id AND item->'fixture'->>'id'=a.fixture_id::text LIMIT 1
) raw ON true
LEFT JOIN live_alert_context c ON c.source='api_football' AND c.source_event_id=a.fixture_id::text
WHERE a.observed_at>=%s AND a.observed_at<=%s
UNION ALL
SELECT 'fonbet',f.fonbet_event_id,f.fixture_id,f.fonbet_event_id,f.competition_name,
 CASE WHEN f.home_away_swapped THEN f.away_name ELSE f.home_name END,
 CASE WHEN f.home_away_swapped THEN f.home_name ELSE f.away_name END,
 f.elapsed,f.observed_at,
 CASE WHEN f.home_away_swapped THEN f.goals_away ELSE f.goals_home END,
 CASE WHEN f.home_away_swapped THEN f.goals_home ELSE f.goals_away END,
 to_jsonb(CASE WHEN f.home_away_swapped THEN f.shots_including_goals_2h_away ELSE f.shots_including_goals_2h_home END),
 to_jsonb(CASE WHEN f.home_away_swapped THEN f.shots_including_goals_2h_home ELSE f.shots_including_goals_2h_away END),
 f.kickoff_at,core.league_id,f.mapping_status,f.mapping_candidates,
 c.observed_at,c.status,c.elapsed,c.present,c.format_tags,f.event_url,f.requested_at,f.context_observed_at,f.run_id::text,NULL::bigint,f.observation_id::text
FROM fonbet_live_statistics_75_normalized f
LEFT JOIN fixtures core ON core.fixture_id=f.fixture_id
LEFT JOIN live_alert_context c ON c.source='fonbet' AND c.source_event_id=f.fonbet_event_id
WHERE f.observed_at>=%s AND f.observed_at<=%s
"""


def rows(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def synchronize(conn):
    """Called under the shared advisory lock, including at restart/cutover."""
    for row in rows(conn, 'SELECT fixture_id,min(sent_at) AS sent_at FROM live_alerts_sent GROUP BY fixture_id'):
        identity = conn.execute("SELECT live_alert_identity('api_football',%s)", (str(row['fixture_id']),)).fetchone()[0]
        conn.execute('UPDATE live_alert_matches SET sent_at=least(sent_at,%s) WHERE alert_match_id=%s', (row['sent_at'],identity))
    for row in rows(conn, "SELECT fonbet_event_id,fixture_id FROM fonbet_live_match_statistics_75 WHERE mapping_status='confirmed'"):
        conn.execute('SELECT live_alert_link(%s,%s)', (row['fonbet_event_id'],row['fixture_id']))
    # A killed sender may have delivered: lease expiry never means safe replay.
    conn.execute("""UPDATE live_alert_matches m SET uncertain_at=now() FROM live_alert_outbox q
     WHERE m.alert_match_id=q.alert_match_id AND q.state='sending' AND q.lease_until<=now()""")
    conn.execute("""UPDATE live_alert_outbox SET state='uncertain',reason='sender_interrupted',updated_at=now()
     WHERE state='sending' AND lease_until<=now()""")


def candidates(conn, now):
    result = defaultdict(list)
    slices = rows(conn,SELECT_SLICES,(now-timedelta(seconds=120),now)*2)
    for row in slices:
        # Dictionary rows use one provider's complete observation only.
        mid=conn.execute('SELECT live_alert_identity(%s,%s)',(row['source'],row['event_id'])).fetchone()[0]
        row['alert_match_id']=mid
        result[mid].append(row)
    # A retained snapshot must not outlive a newer context from any confirmed alias.
    for context in rows(conn,"""SELECT a.alert_match_id,c.* FROM live_alert_aliases a
       JOIN live_alert_context c USING(source,source_event_id) WHERE c.observed_at>=%s AND c.observed_at<=%s""",(now-timedelta(seconds=120),now)):
        # Absence in one provider means unavailable there, not proof the match ended.
        if context['status'] in ('FT','AET','PEN','CANC','ABD','AWD','WO') or (context['status']=='2H' and (context['elapsed'] or 0)>75):
            for row in result.get(context['alert_match_id'],[]):row['present']=False
    return result


def json_ready(row):
    return {k:v.isoformat() if isinstance(v,datetime) else v for k,v in row.items()}


def ambiguous(conn, row):
    if row['source']=='api_football':
        return conn.execute("""SELECT EXISTS(SELECT 1 FROM fonbet_live_match_statistics_75 f
          JOIN live_alert_aliases a ON a.source='fonbet' AND a.source_event_id=f.fonbet_event_id
          JOIN live_alert_matches m USING(alert_match_id)
          WHERE f.mapping_status<>'confirmed' AND f.mapping_candidates @> jsonb_build_array(%s::integer)
           AND (m.sent_at IS NOT NULL OR m.uncertain_at IS NOT NULL))""",(row['fixture_id'],)).fetchone()[0]
    if row['mapping_status']=='confirmed':return False
    if row['mapping_status']=='ambiguous' or row['candidates']:
        return True
    return False


def reserve(conn, mid, row, now):
    state='identity_ambiguous' if ambiguous(conn,row) else 'pending'
    old=conn.execute('SELECT sent_at,uncertain_at,merged_into FROM live_alert_matches WHERE alert_match_id=%s',(mid,)).fetchone()
    if old[0] or old[1] or old[2]:
        return 'already_sent'
    conn.execute("""INSERT INTO live_alert_outbox(alert_match_id,state,payload,next_attempt_at)
     VALUES(%s,%s,%s,%s) ON CONFLICT(alert_match_id) DO UPDATE SET
     payload=EXCLUDED.payload,state=CASE WHEN live_alert_outbox.state='retry' AND EXCLUDED.state='pending' THEN 'retry' ELSE EXCLUDED.state END,
     updated_at=now(),reason=NULL
     WHERE live_alert_outbox.state IN ('pending','retry','expired','identity_ambiguous')""",(mid,state,Jsonb(json_ready(row)),now))
    return state


def message(row, alert_id):
    unknown=lambda v:'нет данных' if v is None else str(v)
    home,away=shots(row['home_shots']),shots(row['away_shots'])
    total=home+away if home is not None and away is not None else None
    observed=row['observed_at']
    if isinstance(observed,str):observed=datetime.fromisoformat(observed)
    from zoneinfo import ZoneInfo
    text=(f"ID: {alert_id}\nТип: удары в створ во 2-м тайме\nЛига: {row['league']}\n"
          f"Матч: {row['home']} — {row['away']}\nМинута: {row['elapsed']}' | Счёт: {unknown(row['goals_home'])}:{unknown(row['goals_away'])}\n"
          f"Во 2-м тайме: {unknown(home)} : {unknown(away)}, суммарно {unknown(total)}\n"
          f"Условие: {'≥6 у команды' if any(v is not None and v>=6 for v in (home,away)) else '≥8 суммарно'}\n"
          f"Источник: {'API-Football' if row['source']=='api_football' else 'Fonbet'} ({row.get('selection_reason','')})\n"
          f"Наблюдение: {observed.astimezone(ZoneInfo('Europe/Moscow')):%d.%m.%Y %H:%M:%S} МСК")
    # A provider's marketing homepage is not a link to the match.
    if row.get('fonbet_event_id'):
        text+='\n'+row['event_url']
    elif row.get('fixture_id'):
        text+=f"\nAPI fixture_id: {row['fixture_id']}"
    markup=None
    if row.get('fixture_id') and row['goals_home'] is not None and row['goals_away'] is not None:
        from src.jobs.live_alerts import bet_keyboard
        markup=bet_keyboard(SimpleNamespace(alert_id=alert_id,categories=('half_pressure',),goals_home=row['goals_home'],goals_away=row['goals_away']))
    elif not row.get('fixture_id'):
        text+='\nЗапись открытой ставки недоступна до подтверждения API-ID матча.'
    return text,markup


class DeliveryFailure(Exception):
    def __init__(self, definite=False):
        self.definite=definite
        super().__init__('telegram_rejected' if definite else 'telegram_delivery_uncertain')


def telegram_sender(token, chat_id, proxy=None, runner=subprocess.run, *, with_receipt=False):
    from src.bot.telegram_api import TelegramApi
    api=TelegramApi(token,proxy)
    def send(text,markup,*,reply_to_message_id=None):
        params={'chat_id':str(chat_id),'text':text}
        if markup is not None:params['reply_markup']=json.dumps(markup,ensure_ascii=False)
        if reply_to_message_id is not None:
            params['reply_parameters']=json.dumps({'message_id':reply_to_message_id,'allow_sending_without_reply':True})
        argv,config=api.request('sendMessage',params,15)
        try:
            proc=runner(argv,input=config,capture_output=True,text=True,timeout=25)
        except (subprocess.TimeoutExpired,OSError):
            raise DeliveryFailure() from None
        if proc.returncode:
            raise DeliveryFailure(definite=proc.returncode in (5,6,7))
        try:body=json.loads(proc.stdout)
        except (ValueError,TypeError):raise DeliveryFailure() from None
        if isinstance(body,dict) and body.get('ok') is False:raise DeliveryFailure(definite=True)
        result=body.get('result') if isinstance(body,dict) and body.get('ok') is True else None
        mid=result.get('message_id') if isinstance(result,dict) else None
        if type(mid) is not int or mid<=0:raise DeliveryFailure()
        if not with_receipt:
            return mid
        timestamp=result.get('date')
        sent_at=None
        if type(timestamp) is int and timestamp>0:
            try:sent_at=datetime.fromtimestamp(timestamp,timezone.utc)
            except (ValueError,OverflowError,OSError):pass
        return DeliveryReceipt(mid,sent_at)
    send.reply=lambda text,mid:send(text,None,reply_to_message_id=mid)
    return send


def record_legacy(conn, row, alert_id):
    if not row.get('fixture_id') or row['goals_home'] is None or row['goals_away'] is None:
        return
    from src.jobs.live_alerts import Alert,_record_sent
    home,away=shots(row['home_shots']),shots(row['away_shots'])
    side='home' if (home if home is not None else -1)>=(away if away is not None else -1) else 'away'
    alert=Alert(fixture_id=row['fixture_id'],league_id=row['league_id'],league=row['league'],country='',
                home=row['home'],away=row['away'],minute=row['elapsed'],goals_home=row['goals_home'],goals_away=row['goals_away'],
                team_side=side,possession=None,shots=home if side=='home' else away,
                opponent_shots=away if side=='home' else home,alert_id=alert_id,category='half_pressure',kickoff_at=row['kickoff_at'])
    _record_sent(conn,alert)


def dispatch(conn,send=None,*,dry_run=False,now_fn=lambda:datetime.now(timezone.utc),limit=30,deadline=None,clock=time.monotonic):
    report=Counter()
    started=clock()
    deadline=started+85 if deadline is None else deadline
    if not conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0]:
        return {'skipped_previous_running':1}
    try:
        with conn.transaction(force_rollback=dry_run):
            synchronize(conn)
            grouped=candidates(conn,now_fn())
            for mid,options in grouped.items():
                selected=choose(options,now_fn())
                if selected is None:report['unusable']+=1;continue
                report['selected_'+selected['source']]+=1
                if pressure(selected['home_shots'],selected['away_shots']) is not True:
                    # A newer negative API result also expires an older queued Fonbet signal.
                    conn.execute("UPDATE live_alert_outbox SET state='expired',reason='trigger_no_longer_true' WHERE alert_match_id=%s AND state IN ('pending','retry','identity_ambiguous')",(mid,))
                    report['not_triggered']+=1;continue
                selected['selection_reason']='api_primary' if selected['source']=='api_football' else 'api_missing_stale_or_unknown'
                if dry_run:report['would_trigger']+=1;continue
                report[reserve(conn,mid,selected,now_fn())]+=1
        if dry_run:return dict(report)
        queue=rows(conn,"""SELECT * FROM live_alert_outbox WHERE state IN ('pending','retry','identity_ambiguous')
         AND next_attempt_at<=%s ORDER BY created_at LIMIT %s""",(now_fn(),limit))
        for job in queue:
            if clock()-started>50 or deadline-clock()<30:
                report['time_budget']+=1;break
            with conn.transaction():
                # Re-read all eligible observations and aliases immediately before network I/O.
                selected=choose(candidates(conn,now_fn()).get(job['alert_match_id'],[]),now_fn())
                match=conn.execute('SELECT sent_at,uncertain_at,merged_into FROM live_alert_matches WHERE alert_match_id=%s FOR UPDATE',(job['alert_match_id'],)).fetchone()
                if match[0] or match[1] or match[2] or selected is None or pressure(selected['home_shots'],selected['away_shots']) is not True:
                    conn.execute("UPDATE live_alert_outbox SET state='expired',reason='no_current_candidate',updated_at=now() WHERE alert_match_id=%s",(job['alert_match_id'],));report['expired']+=1;continue
                if ambiguous(conn,selected):report['identity_ambiguous']+=1;continue
                if deadline-clock()<30:
                    report['time_budget']+=1;break
                selected['selection_reason']='api_primary' if selected['source']=='api_football' else 'api_missing_stale_or_unknown'
                conn.execute("""UPDATE live_alert_outbox SET state='sending',payload=%s,attempts=attempts+1,
                 lease_until=%s,updated_at=now() WHERE alert_match_id=%s""",(Jsonb(json_ready(selected)),now_fn()+timedelta(seconds=90),job['alert_match_id']))
            # Capture enrollment before network I/O; activation never backfills old sends.
            delivery_started_at=now_fn()
            tracking_config=enrollment(conn,delivery_started_at)
            # 'sending' must be committed before a request may reach Telegram.
            try:
                result=send(*message(selected,job['alert_id']))
                receipt=result if isinstance(result,DeliveryReceipt) else DeliveryReceipt(result)
                message_id=receipt.message_id
                if type(message_id) is not int or message_id<=0:raise DeliveryFailure()
            except DeliveryFailure as exc:
                state='retry' if exc.definite else 'uncertain'
                with conn.transaction():
                    conn.execute("UPDATE live_alert_outbox SET state=%s,reason=%s,next_attempt_at=%s,updated_at=now() WHERE alert_match_id=%s",(state,str(exc),now_fn()+timedelta(seconds=15),job['alert_match_id']))
                    if not exc.definite:conn.execute('UPDATE live_alert_matches SET uncertain_at=%s WHERE alert_match_id=%s',(now_fn(),job['alert_match_id']))
                report[state]+=1;continue
            except Exception:
                # An unexpected transport exception must never cause a blind replay.
                with conn.transaction():
                    conn.execute("UPDATE live_alert_outbox SET state='uncertain',reason='transport_exception' WHERE alert_match_id=%s",(job['alert_match_id'],))
                    conn.execute('UPDATE live_alert_matches SET uncertain_at=%s WHERE alert_match_id=%s',(now_fn(),job['alert_match_id']))
                report['uncertain']+=1;continue
            # If this transaction fails, the persisted sending lease recovers as uncertain.
            confirmed_at=now_fn()
            with conn.transaction():
                record_confirmed(conn,signal_id=job['alert_id'],match_id=job['alert_match_id'],
                  receipt=receipt,started_at=delivery_started_at,confirmed_at=confirmed_at,
                  context=json_ready(selected),config=tracking_config)
                record_legacy(conn,selected,job['alert_id'])
                conn.execute('UPDATE live_alert_matches SET sent_at=%s WHERE alert_match_id=%s',(now_fn(),job['alert_match_id']))
                conn.execute("UPDATE live_alert_outbox SET state='sent',telegram_message_id=%s,sent_at=%s,lease_until=NULL,updated_at=now() WHERE alert_match_id=%s",(message_id,now_fn(),job['alert_match_id']))
            report['sent']+=1
        return dict(report)
    finally:
        conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))


def main(argv=None):
    parser=argparse.ArgumentParser(description='Dispatch from saved API/Fonbet live observations')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    # Service supplies environment; the dispatcher never opens .env itself.
    url=os.environ.get('DATABASE_URL')
    if not url:parser.error('DATABASE_URL is required')
    sender=None
    if not args.dry_run:
        token,chat=os.environ.get('TELEGRAM_BOT_TOKEN'),os.environ.get('TELEGRAM_CHAT_ID')
        if not token or not chat:parser.error('Telegram environment is required')
        sender=telegram_sender(token,chat,os.environ.get('TELEGRAM_PROXY') or None,with_receipt=True)
    deadline=time.monotonic()+85
    try:
        with psycopg.connect(url,autocommit=True) as conn:
            if not args.dry_run and not conn.execute('SELECT enabled FROM live_alert_dispatch_config WHERE singleton').fetchone()[0]:
                print(json.dumps({'disabled':True}));return 0
            if args.dry_run:
                # Full evaluation in a rollback-only transaction; no IDs/aliases/queue survive.
                with conn.transaction(force_rollback=True):result=dispatch(conn,None,dry_run=True)
            else:
                result=dispatch(conn,sender,deadline=deadline)
                from src.jobs.signal_reminders import dispatch_reminders
                result.update(dispatch_reminders(conn,sender,deadline=deadline))
        print(json.dumps(result));return 0
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__}));return 1

if __name__=='__main__':raise SystemExit(main())
