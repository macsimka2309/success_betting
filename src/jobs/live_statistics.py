"""One durable 65–75 minute statistics row per API-Football fixture."""
from collections import Counter
from datetime import datetime, timezone
import math

from psycopg.types.json import Jsonb

from src.common.convert import to_int, to_utc
from src.jobs.collect import STAT_COLUMNS, _parse_statistics_row


MINUTE_FROM = 65
MINUTE_TO = 75
MAX_CONTEXT_AGE_SECONDS = 30
CONTEXT_REFRESH_SECONDS = 20
PERIODS = {'match': 'statistics', '1h': 'statistics_1h', '2h': 'statistics_2h'}


def is_statistics_candidate(item):
    status = (item.get('fixture') or {}).get('status') or {}
    minute = to_int(status.get('elapsed'))
    return status.get('short') == '2H' and minute is not None and MINUTE_FROM <= minute <= MINUTE_TO


def normalize_statistics(item, body):
    teams = item.get('teams') or {}
    ids = {side: to_int((teams.get(side) or {}).get('id')) for side in ('home', 'away')}
    if any(team_id is None or team_id <= 0 for team_id in ids.values()) or ids['home'] == ids['away']:
        raise ValueError('invalid_teams')
    rows = body.get('response')
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise ValueError('invalid_response')
    result = {side: dict.fromkeys(PERIODS) for side in ids}
    seen = set()
    for entry in rows:
        if not isinstance(entry, dict) or not isinstance(entry.get('team'), dict):
            raise ValueError('invalid_team')
        team_id = to_int(entry['team'].get('id'))
        if team_id not in ids.values() or team_id in seen:
            raise ValueError('unexpected_team')
        seen.add(team_id)
        side = next(side for side in ids if ids[side] == team_id)
        for period, block in PERIODS.items():
            raw = entry.get(block)
            if raw is not None and (not isinstance(raw, list) or any(not isinstance(r, dict) for r in raw)):
                raise ValueError('invalid_statistics')
            try:
                values = _parse_statistics_row(entry, block)
            except (OverflowError, TypeError, ValueError) as exc:
                raise ValueError('invalid_statistics') from exc
            values = {c: values.get(c) if values.get(c) is not None and math.isfinite(values[c]) else None
                      for c in STAT_COLUMNS}
            if any(v is not None for v in values.values()):
                result[side][period] = values
    populated = sum(any(block is not None for block in periods.values()) for periods in result.values())
    if not populated:
        return None
    return ids, result, 'both_teams' if populated == 2 else 'partial'


def save_statistics(conn, item, body, receipt, context, run_id):
    fixture = item.get('fixture') or {}
    fixture_id = to_int(fixture.get('id'))
    if fixture_id is None or fixture_id <= 0 or not is_statistics_candidate(item):
        raise ValueError('invalid_context')
    supplied_id = (body.get('parameters') or {}).get('fixture')
    if supplied_id is not None and to_int(supplied_id) != fixture_id:
        raise ValueError('unexpected_fixture')
    normalized = normalize_statistics(item, body)
    if normalized is None:
        return 'empty'
    if receipt is None or context is None or receipt.observation_id is None or context.observation_id is None:
        raise ValueError('missing_provenance')
    age = receipt.observed_at - context.observed_at
    if age < 0 or age > MAX_CONTEXT_AGE_SECONDS:
        return 'context_stale'
    ids, statistics, quality = normalized
    status = fixture['status']
    league = item.get('league') or {}
    goals = item.get('goals') or {}
    utc = lambda timestamp: datetime.fromtimestamp(timestamp, timezone.utc)
    values = dict(
        fixture_id=fixture_id, league_id=to_int(league.get('id')), season=to_int(league.get('season')),
        kickoff_at=to_utc(fixture.get('date')), home_team_id=ids['home'], away_team_id=ids['away'],
        status_short=status['short'], elapsed=to_int(status.get('elapsed')),
        elapsed_extra=to_int(status.get('extra')), goals_home=to_int(goals.get('home')),
        goals_away=to_int(goals.get('away')), statistics=Jsonb(statistics),
        requested_at=utc(receipt.requested_at), observed_at=utc(receipt.observed_at),
        context_observed_at=utc(context.observed_at), observation_id=receipt.observation_id,
        context_observation_id=context.observation_id, run_id=run_id,
        minute_basis='context', quality_status=quality,
    )
    columns = list(values)
    updates = ', '.join(f'{c}=EXCLUDED.{c}' for c in columns if c != 'fixture_id')
    sql = f"""INSERT INTO live_match_statistics_75 ({', '.join(columns)})
              VALUES ({', '.join(['%s'] * len(columns))})
              ON CONFLICT (fixture_id) DO UPDATE SET {updates}, updated_at=now()
              WHERE (EXCLUDED.elapsed, EXCLUDED.observed_at, EXCLUDED.observation_id)
                  > (live_match_statistics_75.elapsed, live_match_statistics_75.observed_at,
                     live_match_statistics_75.observation_id)
              RETURNING (xmax = 0)"""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, tuple(values.values()))
        row = cur.fetchone()
        # The analytical projection orders by match minute, whereas reminders
        # need every validated observation in chronological order.
        if receipt.observation_id != context.observation_id:
            cur.execute("""INSERT INTO live_api_reminder_observations
              (observation_id,context_observation_id,fixture_id,observed_at)
              VALUES (%s,%s,%s,%s) ON CONFLICT (observation_id) DO NOTHING""",
              (receipt.observation_id,context.observation_id,fixture_id,values['observed_at']))
    return 'older_or_duplicate' if row is None else ('inserted' if row[0] else 'updated')


def collect_statistics(client, conn, items, context, run_id, log=print):
    """Fetch once for each eligible game before alert filters; reuse bodies for alerts."""
    candidates = [item for item in items if is_statistics_candidate(item)]
    responses = {}
    current_items = {to_int((item.get('fixture') or {}).get('id')): item for item in candidates}
    counts = Counter(selected=len(candidates))
    for item in items:
        status = (item.get('fixture') or {}).get('status') or {}
        if status.get('short') == '2H' and to_int(status.get('elapsed')) is None:
            counts['invalid_context'] += 1
    for item in candidates:
        fixture_id = to_int((item.get('fixture') or {}).get('id'))
        if fixture_id is None or fixture_id <= 0:
            counts['invalid_context'] += 1
            continue
        if fixture_id in responses:
            continue
        if client.quota_exhausted or client.requests_used >= client.max_requests:
            counts['budget_exceeded'] += 1
            continue
        if context is not None and client.now() - context.observed_at >= CONTEXT_REFRESH_SECONDS:
            refreshed = client.get('/fixtures', {'live': 'all'}, use_cache=False)
            counts['context_requests'] += 1
            if refreshed is None or refreshed.get('errors'):
                counts['api_error'] += 1
                continue
            context = client.last_observation
            from src.jobs.live_context import record_api_context
            record_api_context(conn, refreshed.get('response') or [], context)
            current_items = {to_int((match.get('fixture') or {}).get('id')): match
                             for match in refreshed.get('response') or []}
        item = current_items.get(fixture_id)
        if item is None:
            counts['outside_window'] += 1
            continue
        if not is_statistics_candidate(item):
            counts['outside_window'] += 1
            continue
        if client.quota_exhausted or client.requests_used >= client.max_requests:
            counts['budget_exceeded'] += 1
            continue
        body = client.get('/fixtures/statistics', {'fixture': fixture_id, 'half': 'true'}, use_cache=False)
        counts['requests'] += 1
        responses[fixture_id] = body
        if body is None or body.get('errors'):
            counts['api_error'] += 1
            continue
        try:
            outcome = save_statistics(conn, item, body, client.last_observation, context, run_id)
        except ValueError as exc:
            outcome = 'invalid_context'
            responses[fixture_id] = None
            log(f'[live-statistics-75] fixture_id={fixture_id} reason={exc}')
        if outcome in ('inserted', 'updated', 'older_or_duplicate', 'context_stale'):
            counts['nonempty'] += 1
        counts[outcome] += 1
    log(f'[live-statistics-75] run_id={run_id} {dict(counts)}')
    return responses
