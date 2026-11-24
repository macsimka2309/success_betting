"""Conservative source matching and an auditable API-first analytical view."""
import argparse
from contextlib import nullcontext
import fcntl
import json
from pathlib import Path
import subprocess
import unicodedata
from datetime import timedelta
from uuid import UUID

from store import sql_literal, utc

METHOD = 'strict_identity_v1'
BACKLOG_BATCH = 100


def name_key(value):
    text = unicodedata.normalize('NFKC', value or '').casefold()
    return ' '.join(''.join(char if char.isalnum() else ' ' for char in text).split())


def same_identity(name, api_name, api_id, aliases):
    key = name_key(name)
    return bool(key) and (key == name_key(api_name) or aliases.get(key) == api_id)


def choose_match(event, fixtures, team_aliases, league_aliases):
    candidates = []
    eligible = []
    for fixture in fixtures:
        direct = (same_identity(event['home_name'], fixture['home_name'], fixture['home_team_id'], team_aliases)
                  and same_identity(event['away_name'], fixture['away_name'], fixture['away_team_id'], team_aliases))
        reverse = (same_identity(event['home_name'], fixture['away_name'], fixture['away_team_id'], team_aliases)
                   and same_identity(event['away_name'], fixture['home_name'], fixture['home_team_id'], team_aliases))
        if not (direct or reverse):
            continue
        candidates.append(fixture['fixture_id'])
        # A collapsed identity could fit both orientations. Never guess its ordering.
        if direct and reverse:
            continue
        if not same_identity(event['competition_name'], fixture['league_name'], fixture['league_id'], league_aliases):
            continue
        swapped = reverse
        if event.get('kickoff_at'):
            compatible = abs(utc(event['kickoff_at']) - utc(fixture['kickoff_at'])) <= timedelta(minutes=10)
        else:
            snapshot = fixture.get('snapshot')
            if not snapshot:
                continue
            home = event.get('goals_away') if swapped else event.get('goals_home')
            away = event.get('goals_home') if swapped else event.get('goals_away')
            compatible = (home is not None and away is not None
                          and home == snapshot.get('goals_home') and away == snapshot.get('goals_away')
                          and abs(event['elapsed'] - snapshot['elapsed']) <= 3
                          and abs(utc(event['observed_at']) - utc(snapshot['observed_at'])) <= timedelta(seconds=120))
        if compatible:
            eligible.append((fixture['fixture_id'], swapped))
    if len(eligible) == 1:
        fixture_id, swapped = eligible[0]
        return dict(fixture_id=fixture_id, mapping_status='confirmed', home_away_swapped=swapped,
                    mapping_reason='unique_teams_competition_time', mapping_candidates=sorted(set(candidates)))
    return dict(fixture_id=None, mapping_status='ambiguous' if len(eligible) > 1 else 'unmatched',
                home_away_swapped=False,
                mapping_reason='multiple_compatible_fixtures' if len(eligible) > 1 else 'identity_or_time_unconfirmed',
                mapping_candidates=sorted(set(candidates)))


def decision_sql(event, decision, method=METHOD):
    row = dict(decision, fonbet_event_id=event['fonbet_event_id'], observation_id=event['observation_id'],
               mapping_method=method)
    return f"""WITH decision AS (
 SELECT * FROM jsonb_to_record({sql_literal(row)}) AS d(
 fonbet_event_id text, observation_id uuid, fixture_id integer, mapping_status text,
 home_away_swapped boolean, mapping_reason text, mapping_candidates jsonb, mapping_method text)
), changed AS (
 UPDATE fonbet_live_match_statistics_75 f SET
 fixture_id=d.fixture_id, mapping_status=d.mapping_status, home_away_swapped=d.home_away_swapped,
 mapping_reason=d.mapping_reason,mapping_candidates=d.mapping_candidates,
 mapping_method=d.mapping_method,mapped_at=now()
 FROM decision d WHERE f.fonbet_event_id=d.fonbet_event_id AND f.observation_id=d.observation_id
 AND f.mapping_status <> 'confirmed'
 AND (f.fixture_id,f.mapping_status,f.home_away_swapped,f.mapping_reason,f.mapping_candidates,f.mapping_method)
  IS DISTINCT FROM (d.fixture_id,d.mapping_status,d.home_away_swapped,d.mapping_reason,d.mapping_candidates,d.mapping_method)
 RETURNING f.*
)
INSERT INTO fonbet_fixture_mapping_audit(
 fonbet_event_id,observation_id,fixture_id,mapping_status,home_away_swapped,reason,candidates,method)
SELECT fonbet_event_id,observation_id,fixture_id,mapping_status,home_away_swapped,
 mapping_reason,mapping_candidates,mapping_method FROM changed;
"""


DATA_SQL = """BEGIN READ ONLY;
SET LOCAL statement_timeout='30s';
WITH pending AS MATERIALIZED (
 SELECT * FROM fonbet_live_match_statistics_75 WHERE mapping_status<>'confirmed'
 -- pending scope
), bounds AS (
 SELECT (min(least(observed_at-interval '4 hours',kickoff_at-interval '10 minutes')) AT TIME ZONE 'UTC')::date-1 AS date_from,
        (max(greatest(observed_at,kickoff_at+interval '10 minutes')) AT TIME ZONE 'UTC')::date+1 AS date_to
 FROM pending
)
SELECT json_build_object(
 'events',COALESCE((SELECT json_agg(f) FROM pending f),'[]'),
 'fixtures',COALESCE((SELECT json_agg(candidate) FROM (
  SELECT f.fixture_id,f.home_team_id,f.away_team_id,f.league_id,f.kickoff_at,
   th.name AS home_name,ta.name AS away_name,l.name AS league_name,
   CASE WHEN s.fixture_id IS NOT NULL THEN json_build_object(
    'observed_at',s.observed_at,'elapsed',s.elapsed,'goals_home',s.goals_home,'goals_away',s.goals_away) END AS snapshot
  FROM fixtures f JOIN bounds b ON f.match_date BETWEEN b.date_from AND b.date_to
  JOIN teams th ON th.team_id=f.home_team_id JOIN teams ta ON ta.team_id=f.away_team_id
  JOIN leagues l USING(league_id) LEFT JOIN live_match_statistics_75 s USING(fixture_id)
  WHERE EXISTS (SELECT 1 FROM pending fb WHERE (f.kickoff_at BETWEEN fb.observed_at-interval '4 hours' AND fb.observed_at
     OR f.kickoff_at BETWEEN fb.kickoff_at-interval '10 minutes' AND fb.kickoff_at+interval '10 minutes'))
  UNION ALL
  SELECT s.fixture_id,s.home_team_id,s.away_team_id,s.league_id,s.kickoff_at,
   item->'teams'->'home'->>'name', item->'teams'->'away'->>'name',item->'league'->>'name',
   json_build_object('observed_at',s.observed_at,'elapsed',s.elapsed,
                     'goals_home',s.goals_home,'goals_away',s.goals_away)
  FROM live_match_statistics_75 s
  JOIN api_observations o ON o.observation_id=s.context_observation_id
  CROSS JOIN LATERAL jsonb_array_elements(o.payload->'response') item
  WHERE EXISTS (SELECT 1 FROM pending)
   AND NOT EXISTS (SELECT 1 FROM fixtures f WHERE f.fixture_id=s.fixture_id)
   AND item->'fixture'->>'id'=s.fixture_id::text
 ) candidate),'[]'),
 'team_aliases',COALESCE((SELECT json_object_agg(alias_key,team_id) FROM fonbet_team_aliases),'{}'),
 'league_aliases',COALESCE((SELECT json_object_agg(alias_key,league_id) FROM fonbet_competition_aliases),'{}'));
COMMIT;
"""


def database(sql):
    command = ['sudo','-n','docker','exec','-i','football-postgres','sh','-c',
               'exec psql -X -qAt -U "$POSTGRES_USER" -d football -v ON_ERROR_STOP=1 -f -']
    try:
        return subprocess.run(command, input=sql, text=True, capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        code = 'matching_query_timeout' if 'statement timeout' in (exc.stderr or '') else 'matching_database_error'
        raise RuntimeError(code) from None


def pending_sql(*, run_id=None, after=None):
    if run_id is not None:
        scope = f"AND run_id='{UUID(run_id)}'::uuid"
    elif after is not None:
        if after and (not after.isascii() or not after.isdecimal()):
            raise ValueError('invalid_backlog_cursor')
        scope = f"AND fonbet_event_id > '{after}' ORDER BY fonbet_event_id LIMIT {BACKLOG_BATCH}"
    else:
        return DATA_SQL
    return DATA_SQL.replace('-- pending scope', scope)


def reconcile(query, apply):
    data = json.loads(database(query).strip())
    statements = []
    summary = dict(confirmed=0, unmatched=0, ambiguous=0)
    for event in data['events']:
        decision = choose_match(event, data['fixtures'], data['team_aliases'], data['league_aliases'])
        summary[decision['mapping_status']] += 1
        statements.append(decision_sql(event, decision))
    if apply and statements:
        database("BEGIN; SET LOCAL standard_conforming_strings=on; SET LOCAL lock_timeout='3s'; "
                 "SET LOCAL statement_timeout='30s';\n"
                 + '\n'.join(statements) + '\nCOMMIT;')
    return summary, data['events']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--input', type=Path, help='Only reconcile retained snapshots written by this export')
    scope.add_argument('--backlog-state', type=Path, help='Reconcile one batch and resume from this cursor')
    args = parser.parse_args()
    run_id = json.loads(args.input.read_text())['run_id'] if args.input else None
    state = args.backlog_state
    with state.with_suffix('.lock').open('a') if state else nullcontext() as lock:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        after = (state.read_text().strip() if state.exists() else '') if state else None
        summary, events = reconcile(pending_sql(run_id=run_id, after=after), args.apply)
        if state and args.apply:
            # A short batch completes this pass. Unmatched rows can be retried on the next pass.
            cursor = max(event['fonbet_event_id'] for event in events) if len(events) == BACKLOG_BATCH else ''
            next_state = state.with_suffix('.tmp')
            next_state.write_text(cursor + '\n')
            next_state.replace(state)
    label = 'export' if args.input else 'backlog' if state else 'all'
    print(json.dumps(dict(mapping=summary, applied=args.apply, scope=label, scanned=len(events))))


if __name__ == '__main__':
    main()
