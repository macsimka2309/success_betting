"""Read validated same-source polls without altering the retained 75-minute snapshot."""
from collections import defaultdict
from datetime import timedelta

from src.jobs.alert_dispatch import rows
from src.jobs.alert_rules import TTL, usable
from src.common.convert import to_int
from src.jobs.live_statistics import MAX_CONTEXT_AGE_SECONDS, is_statistics_candidate, normalize_statistics
from tools.fonbet_live.match_clock import api_period


FONBET_POLLS = """
SELECT o.observation_id::text AS statistics_observation_id,
       o.run_id::text AS statistics_poll_id, NULL::bigint AS statistics_collection_run_id,
       r.started_at AS context_requested_at, o.observed_at,
       p.requested_at AS statistics_requested_at, p.context_observed_at AS context_at,
       p.elapsed,p.period,p.competition_name AS league,p.format_tags,
       CASE WHEN f.home_away_swapped THEN metric.away ELSE metric.home END AS home_shots,
       CASE WHEN f.home_away_swapped THEN metric.home ELSE metric.away END AS away_shots
FROM fonbet_live_observations o JOIN fonbet_live_runs r USING (run_id)
LEFT JOIN fonbet_live_match_statistics_75 f USING (fonbet_event_id)
CROSS JOIN LATERAL jsonb_to_record(o.payload) AS p(
    requested_at timestamptz, context_observed_at timestamptz, elapsed integer, period text,
    competition_name text, format_tags jsonb, shots_on_target_2h_home integer,
    shots_on_target_2h_away integer, goals_home integer, goals_away integer, ht_home integer, ht_away integer
)
CROSS JOIN LATERAL (
    SELECT p.shots_on_target_2h_home+fonbet_second_half_goals(p.goals_home,p.ht_home) AS home,
           p.shots_on_target_2h_away+fonbet_second_half_goals(p.goals_away,p.ht_away) AS away
) metric
WHERE o.fonbet_event_id=%s AND o.status='ok' AND o.observed_at>%s AND o.observed_at<=%s
ORDER BY o.observed_at,o.observation_id
"""

API_POLLS = """
WITH explicit_bindings AS (
    SELECT DISTINCT ON (observation_id) observation_id,context_observation_id
    FROM (
        SELECT observation_id,context_observation_id,0 AS priority
        FROM live_api_reminder_observations
        WHERE fixture_id=%(fixture)s AND observed_at>%(since)s AND observed_at<=%(now)s
        UNION ALL
        SELECT observation_id,context_observation_id,1
        FROM live_match_statistics_75
        WHERE fixture_id=%(fixture)s AND observed_at>%(since)s AND observed_at<=%(now)s
    ) candidates
    ORDER BY observation_id,priority
), bindings AS (
    SELECT observation_id,context_observation_id,false AS recovered FROM explicit_bindings
    UNION ALL
    SELECT s.observation_id,c.observation_id,true
    FROM api_observations s
    JOIN collection_runs r ON r.run_id=s.run_id AND r.job_name='live_alerts'
    JOIN api_observations c ON c.run_id=s.run_id
        AND c.endpoint='/fixtures' AND c.params->>'live'='all'
        AND c.observed_at<=s.requested_at
        AND c.observed_at>=s.observed_at-%(context_age)s
    WHERE s.endpoint='/fixtures/statistics' AND s.params->>'fixture'=%(event_id)s
        AND s.params->>'half'='true'
        AND s.observed_at>%(since)s AND s.observed_at<=%(now)s
        AND s.requested_at>%(confirmed)s AND s.requested_at<=s.observed_at
        AND NOT EXISTS (SELECT 1 FROM explicit_bindings e WHERE e.observation_id=s.observation_id)
)
SELECT s.observation_id::text AS statistics_observation_id, c.observation_id::text AS statistics_poll_id,
       c.run_id AS statistics_collection_run_id, s.requested_at AS statistics_requested_at,
       s.observed_at,c.requested_at AS context_requested_at,c.observed_at AS context_at,
       s.payload AS statistics,c.payload AS context,b.recovered
FROM bindings b
JOIN api_observations s ON s.observation_id=b.observation_id
JOIN api_observations c ON c.observation_id=b.context_observation_id
WHERE s.endpoint='/fixtures/statistics' AND c.endpoint='/fixtures'
  AND c.params->>'live'='all' AND c.run_id=s.run_id
  AND s.params->>'fixture'=%(event_id)s AND c.observed_at<=s.requested_at
  AND s.observed_at>%(since)s AND s.observed_at<=%(now)s
ORDER BY s.observed_at,s.observation_id,c.observed_at DESC,c.observation_id DESC
"""


def successful_live_response(context):
    return (isinstance(context, dict) and not context.get('errors')
            and isinstance(context.get('response'), list))


def api_context_match(context, event_id):
    if not successful_live_response(context):
        return None
    matches = [item for item in context['response']
               if isinstance(item,dict) and isinstance(item.get('fixture'),dict)
               and str(item['fixture'].get('id'))==event_id]
    if len(matches) != 1:
        return None
    match = matches[0]
    if not isinstance(match['fixture'].get('status'), dict):
        return None
    teams = match.get('teams')
    if not isinstance(teams,dict) or any(not isinstance(teams.get(side),dict) for side in ('home','away')):
        return None
    return match


def api_match_team_ids(match):
    if match is None:
        return None
    try:
        home, away = (to_int(match['teams'][side].get('id')) for side in ('home', 'away'))
    except OverflowError:
        return None
    if home is None or away is None or min(home, away) <= 0 or home == away:
        return None
    return home, away


def current_api_team_ids(context, payload, requested_at, observed_at):
    """Validate the live projection against the archived reply that produced it."""
    if (not context['present'] or requested_at is None or observed_at is None
            or context['requested_at'] != requested_at
            or context['observed_at'] != observed_at or context['completed_at'] != observed_at):
        return None
    match = api_context_match(payload, context['source_event_id'])
    if match is None:
        return None
    status = match['fixture']['status']
    short = status.get('short')
    try:
        if (short != context['status'] or api_period(short) != context['period']
                or to_int(status.get('elapsed')) != context['elapsed']
                or to_int(status.get('extra')) != context['added_minute']):
            return None
    except OverflowError:
        return None
    return api_match_team_ids(match)


def api_values(row, event_id):
    context, statistics = row.pop('context'), row.pop('statistics')
    if not isinstance(statistics, dict) or statistics.get('errors'):
        return None
    parameters = statistics.get('parameters')
    if isinstance(parameters,dict) and parameters.get('fixture') is not None:
        if to_int(parameters['fixture'])!=int(event_id):
            return None
    match = api_context_match(context, event_id)
    if match is None or not is_statistics_candidate(match):
        return None
    try:
        normalized = normalize_statistics(match, statistics)
    except ValueError:
        return None
    if normalized is None:
        return None
    ids, values, _ = normalized
    league = match.get('league')
    return dict(row, period='2H', elapsed=to_int(match['fixture']['status']['elapsed']),
                league=league.get('name') if isinstance(league,dict) else None,
                api_team_ids=(ids['home'], ids['away']),
                home_shots=(values['home']['2h'] or {}).get('shots_on_goal'),
                away_shots=(values['away']['2h'] or {}).get('shots_on_goal'))


def archived_id(value):
    value = str(value)
    if value.isascii() and value.isdecimal() and len(value) <= 18:
        return int(value)
    return None


def expected_api_teams(conn, job):
    """Resolve expected source-side teams independently of the current live reply."""
    expected = {
        event: (home, away) for event, home, away in conn.execute("""
            SELECT a.source_event_id,f.home_team_id,f.away_team_id
            FROM live_alert_aliases a JOIN fixtures f ON f.fixture_id=CASE
              WHEN a.source_event_id ~ '^[0-9]{1,10}$' THEN a.source_event_id::bigint END
            WHERE a.alert_match_id=%s AND a.source='api_football'
              AND f.home_team_id>0 AND f.away_team_id>0 AND f.home_team_id<>f.away_team_id
        """, (job['alert_match_id'],))
    }
    if job['first_source'] != 'api_football':
        return expected

    event_id = job['first_source_event_id']
    initial_poll_id = archived_id(job['first_context'].get('statistics_poll_id'))
    if initial_poll_id is not None:
        initial = conn.execute("""
            SELECT payload FROM api_observations WHERE observation_id=%s
              AND endpoint='/fixtures' AND params->>'live'='all' AND observed_at<=%s
        """, (initial_poll_id, job['confirmed_at'])).fetchone()
        initial_teams = api_match_team_ids(api_context_match(initial[0], event_id)) if initial else None
        if initial_teams is not None:
            if expected.get(event_id, initial_teams) == initial_teams:
                expected[event_id] = initial_teams
            else:
                expected.pop(event_id)
            return expected

    first = job['first_decision'] or {}
    if (event_id not in expected and first.get('source') == 'api_football'
            and first.get('source_event_id') == event_id):
        evidence = rows(conn, """
            SELECT c.payload AS context,s.payload AS statistics
            FROM api_observations s JOIN api_observations c ON c.run_id=s.run_id
            WHERE c.observation_id=%s AND s.observation_id=%s
              AND c.endpoint='/fixtures' AND c.params->>'live'='all'
              AND s.endpoint='/fixtures/statistics' AND s.params->>'fixture'=%s
              AND s.params->>'half'='true'
              AND %s<c.requested_at AND c.requested_at<=c.observed_at
              AND c.observed_at<=s.requested_at AND s.requested_at<=s.observed_at
              AND s.observed_at-c.observed_at<=%s
        """, (archived_id(first.get('poll_id')), archived_id(first.get('observation_id')), event_id,
              job['confirmed_at'], timedelta(seconds=MAX_CONTEXT_AGE_SECONDS)))
        poll = api_values(evidence[0], event_id) if evidence else None
        if poll is not None:
            expected[event_id] = poll['api_team_ids']
    return expected


def available_polls(conn, job, now):
    confirmed = job['confirmed_at']
    since = max(confirmed,now-TTL) if job['first_decision'] is not None else confirmed
    event_id = job['first_source_event_id']
    source = job['first_source']
    context_age = timedelta(seconds=MAX_CONTEXT_AGE_SECONDS)
    latest_context = {}
    if source == 'fonbet':
        observations = rows(conn,FONBET_POLLS,(event_id,since,now))
    else:
        # The complete wait window is used for the first decision. After it
        # is recorded only fresh observations can authorize delivery.
        params = dict(fixture=int(event_id),event_id=event_id,since=since,now=now,
                      confirmed=confirmed,context_age=context_age)
        observations = []
        for row in rows(conn, API_POLLS, params):
            if row['recovered']:
                if not successful_live_response(row['context']):
                    continue
                # Newest first for this statistics response. Record it before
                # validation so a missing or ambiguous fixture blocks fallback.
                latest_context.setdefault(row['statistics_observation_id'], row['statistics_poll_id'])
            observations.append(api_values(row, event_id))
    valid = []
    recovered = defaultdict(list)
    for poll in observations:
        if not poll or poll['period']!='2H' or poll['context_at'] is None or poll['statistics_requested_at'] is None:
            continue
        if not (confirmed < poll['context_requested_at'] <= poll['context_at'] <= poll['observed_at']
                and poll['context_requested_at'] <= poll['statistics_requested_at'] <= poll['observed_at']
                and poll['observed_at']-poll['context_at'] <= context_age):
            continue
        poll.update(source=source,event_id=event_id,status='2H',present=True,current_minute=poll['elapsed'],
                    statistics_context_at=poll['context_at'])
        if source=='api_football':
            poll['league'] = job['first_context'].get('league') or poll['league']
        if poll.get('recovered'):
            # Count plausible contexts even when their trigger is unknown.
            # Such a response cannot establish which context the collector used.
            recovered[poll['statistics_observation_id']].append(poll)
        elif usable(poll,poll['observed_at']):
            valid.append(poll)
    for observation_id, candidates in recovered.items():
        if len(candidates) == 1:
            poll = candidates[0]
            if poll['statistics_poll_id'] == latest_context[observation_id] and usable(poll, poll['observed_at']):
                valid.append(poll)
    return valid
