"""Validate and persist sports-only browser exports; never loads .env or browser profile."""
import argparse
import json
import re
import subprocess
from datetime import datetime
from uuid import UUID

COLUMNS = ('fonbet_event_id event_url home_name away_name competition_name kickoff_at period elapsed clock_text '
           'goals_home goals_away ht_home ht_away shots_on_target_2h_home shots_on_target_2h_away source_period_label '
           'source_metric_label discovered_at requested_at observed_at context_observed_at minute_basis '
           'quality_status observation_id run_id').split()


def utc(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('timezone_required')
    return result


def validate(row):
    UUID(row['observation_id']); UUID(row['run_id'])
    if not re.fullmatch(r'\d+', row['fonbet_event_id']):
        raise ValueError('invalid_event_id')
    if not re.fullmatch(r'https://fon\.bet/live/football/(?:(?:country/[A-Za-z0-9-]+|category/(?:internationalteams|internationalclubs))/)?\d+/'+row['fonbet_event_id']+r'/?', row['event_url']):
        raise ValueError('invalid_event_url')
    if not row['home_name'] or not row['away_name'] or row['home_name']==row['away_name']:
        raise ValueError('invalid_teams')
    if row['period']!='2H' or type(row['elapsed']) is not int or not 65<=row['elapsed']<=75:
        raise ValueError('outside_window')
    if row['source_period_label'].casefold() not in ('2 тайм','2-й тайм','второй тайм') or row['source_metric_label'] not in ('Удары в створ','Ударов в створ'):
        raise ValueError('invalid_period_or_metric')
    if row['minute_basis'] not in ('match_center','list_context'):
        raise ValueError('invalid_context')
    observed, context, requested = (utc(row[k]) for k in ('observed_at','context_observed_at','requested_at'))
    if not 0<=(observed-context).total_seconds()<=30 or requested>observed:
        raise ValueError('context_stale')
    utc(row['discovered_at'])
    values=[row[k] for k in ('shots_on_target_2h_home','shots_on_target_2h_away')]
    if all(v is None for v in values):
        raise ValueError('empty')
    if any(v is not None and (type(v) is not int or not 0<=v<=32767) for v in values):
        raise ValueError('invalid_shots')
    if row['quality_status'] != ('both_teams' if None not in values else 'partial'):
        raise ValueError('invalid_quality')
    for side in ('home', 'away'):
        ht, goals = row.get('ht_'+side), row.get('goals_'+side)
        if ht is not None and (type(ht) is not int or not 0 <= ht <= 32767
                               or type(goals) is not int or ht > goals):
            raise ValueError('invalid_half_score')
    # Unreviewed browser input cannot assign an API fixture or overwrite a mapping.
    return {key:row.get(key) for key in COLUMNS}


def sql_literal(value):
    return "'" + json.dumps(value, ensure_ascii=False, allow_nan=False).replace("'", "''") + "'::jsonb"



def build_context_sql(data):
    if 'contexts' not in data:
        return ''  # Compatibility with archived exports and older collector releases.
    contexts=[]
    for raw in data['contexts']:
        eid=str(raw['fonbet_event_id'])
        if not re.fullmatch(r'\d+',eid):raise ValueError('invalid_context_event')
        observed=utc(raw['observed_at']).isoformat()
        try:
            from .match_clock import fonbet_clock
        except ImportError:  # standalone deployment entry point
            from match_clock import fonbet_clock
        status,minute,extra,period=fonbet_clock(raw.get('clock_text'),raw.get('period_text'))
        contexts.append(dict(source='fonbet',source_event_id=eid,observed_at=observed,status=status,
                             elapsed=minute,present=True,format_tags=raw.get('format_tags') or [],
                             poll_id=str(data.get('run_id') or '') or None,
                             requested_at=data.get('started_at'),completed_at=observed,
                             period=period,added_minute=extra))
    sql=f"""INSERT INTO live_alert_context SELECT * FROM jsonb_populate_recordset(NULL::live_alert_context,{sql_literal(contexts)})
    ON CONFLICT(source,source_event_id) DO UPDATE SET observed_at=EXCLUDED.observed_at,status=EXCLUDED.status,
    elapsed=EXCLUDED.elapsed,present=true,format_tags=EXCLUDED.format_tags,
    poll_id=EXCLUDED.poll_id,requested_at=EXCLUDED.requested_at,completed_at=EXCLUDED.completed_at,
    period=EXCLUDED.period,added_minute=EXCLUDED.added_minute
    WHERE EXCLUDED.observed_at >= live_alert_context.observed_at;
"""
    if data.get('context_complete'):
        scan=utc(data['context_scan_at']).isoformat().replace("'","''")
        # Compare disappearance against the run's start: a concurrent newer observation wins.
        started=utc(data['started_at']).isoformat().replace("'","''")
        ids=sql_literal([c['source_event_id'] for c in contexts])
        sql+=f"""UPDATE live_alert_context SET present=false,status='absent',observed_at='{scan}'::timestamptz
        WHERE source='fonbet' AND observed_at<'{started}'::timestamptz
        AND source_event_id NOT IN (SELECT jsonb_array_elements_text({ids}));
"""
    return sql

def build_sql(data):
    run_id=str(UUID(data['run_id']))
    utc(data['started_at']); utc(data['finished_at'])
    if data['status'] not in ('complete','partial','failed'):
        raise ValueError('invalid_run_status')
    observations=[]; snapshots=[]
    for raw in data['observations']:
        oid=str(UUID(raw['observation_id']))
        if raw['run_id']!=run_id:
            raise ValueError('run_mismatch')
        observed=utc(raw['observed_at']).isoformat()
        if not re.fullmatch(r'\d+', raw['fonbet_event_id']):
            raise ValueError('invalid_event_id')
        # Whitelist prevents unrelated DOM, account contents or error traces entering DB.
        allowed=set(COLUMNS)|{'status','reason','period_text','format_tags','home_value','away_value','period_confirmed'}
        payload={k:v for k,v in raw.items() if k in allowed}
        observations.append(dict(observation_id=oid,run_id=run_id,fonbet_event_id=raw['fonbet_event_id'],
                                 observed_at=observed,status=raw['status'],payload=payload))
        if raw['status']=='ok':
            snapshots.append(validate(raw))
    run={k:data[k] for k in ('run_id','started_at','finished_at','status','summary')}
    columns=', '.join(COLUMNS)
    updates=', '.join(f'{c}=EXCLUDED.{c}' for c in COLUMNS if c!='fonbet_event_id')
    return f"""BEGIN;
SET LOCAL standard_conforming_strings = on;
{build_context_sql(data)}
INSERT INTO fonbet_live_runs SELECT * FROM jsonb_populate_record(NULL::fonbet_live_runs,{sql_literal(run)})
ON CONFLICT (run_id) DO NOTHING;
INSERT INTO fonbet_live_observations SELECT * FROM jsonb_populate_recordset(NULL::fonbet_live_observations,{sql_literal(observations)})
ON CONFLICT (observation_id) DO NOTHING;
WITH incoming AS MATERIALIZED (
 SELECT {columns} FROM jsonb_populate_recordset(NULL::fonbet_live_match_statistics_75,{sql_literal(snapshots)})
), corrections AS MATERIALIZED (
 SELECT i.fonbet_event_id FROM incoming i JOIN fonbet_live_match_statistics_75 old USING(fonbet_event_id)
 WHERE (i.shots_on_target_2h_home < old.shots_on_target_2h_home OR i.shots_on_target_2h_away < old.shots_on_target_2h_away)
), written AS (
 INSERT INTO fonbet_live_match_statistics_75 ({columns})
 SELECT {columns} FROM incoming
 ON CONFLICT (fonbet_event_id) DO UPDATE SET {updates},updated_at=now()
 WHERE (EXCLUDED.elapsed,EXCLUDED.observed_at,EXCLUDED.observation_id) >
       (fonbet_live_match_statistics_75.elapsed,fonbet_live_match_statistics_75.observed_at,fonbet_live_match_statistics_75.observation_id)
 RETURNING (xmax=0) AS inserted, fonbet_event_id
)
SELECT json_build_object('inserted',count(*) FILTER(WHERE inserted),'updated',count(*) FILTER(WHERE NOT inserted),
 'source_corrections',(SELECT count(*) FROM corrections JOIN written USING(fonbet_event_id))) FROM written;
COMMIT;
"""


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',required=True)
    ap.add_argument('--apply',action='store_true')
    ap.add_argument('--sql',action='store_true',help='Emit validated SQL for isolated integration tests')
    args=ap.parse_args()
    with open(args.input) as f:data=json.load(f)
    sql=build_sql(data)
    if args.sql:
        print(sql);return
    result={'run_id':data['run_id'],'status':data['status'],'summary':data['summary'],'applied':args.apply}
    if args.apply:
        cmd=['sudo','-n','docker','exec','-i','football-postgres','sh','-c',
             'exec psql -X -qAt -U "$POSTGRES_USER" -d football -v ON_ERROR_STOP=1 -f -']
        response=subprocess.run(cmd,input=sql,text=True,capture_output=True,check=True)
        result['storage']=[json.loads(line) for line in response.stdout.splitlines() if line.startswith('{')]
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
