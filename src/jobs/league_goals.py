"""League scoring context from the local API-Football catalog."""

import unicodedata
from datetime import datetime, timedelta, timezone

from src.common.convert import to_utc


def average_regulation_goals(conn, league_id, season, before):
    average, matches = conn.execute("""
        SELECT round(avg(goals), 2), count(goals)
        FROM (
          SELECT CASE
            WHEN status_short = 'FT' AND goals_home >= 0 AND goals_away >= 0
              THEN goals_home + goals_away
            WHEN status_short IN ('AET', 'PEN') AND ft90_home >= 0 AND ft90_away >= 0
              THEN ft90_home + ft90_away
          END AS goals
          FROM fixtures
          WHERE league_id = %s AND season = %s AND kickoff_at < %s
            AND status_short IN ('FT', 'AET', 'PEN')
        ) completed
    """, (league_id, season, before)).fetchone()
    if not matches:
        return None
    return {'average': str(average), 'matches': matches, 'season': season}


def league_goals(conn, *, fixture_id=None, league_id=None, season=None,
                 fonbet_competition=None, kickoff_at=None, observed_at=None, before=None):
    """Resolve the season without guessing a league from a similar name."""
    if conn is None:
        return None
    if fixture_id is not None:
        fixture = conn.execute('SELECT league_id, season FROM fixtures WHERE fixture_id=%s',
                               (fixture_id,)).fetchone()
        if fixture:
            if (league_id not in (None, fixture[0]) or season not in (None, fixture[1])):
                return None
            league_id, season = fixture
    if league_id is None and fonbet_competition:
        # Approved aliases use the same normalization as the Fonbet reconciler.
        name = unicodedata.normalize('NFKC', fonbet_competition).casefold()
        alias_key = ' '.join(''.join(char if char.isalnum() else ' ' for char in name).split())
        alias = conn.execute('SELECT league_id FROM fonbet_competition_aliases WHERE alias_key=%s',
                             (alias_key,)).fetchone()
        if alias:
            league_id = alias[0]
    if league_id is None:
        return None
    if season is None:
        kickoff = to_utc(kickoff_at)
        observed = to_utc(observed_at) if fonbet_competition else None
        if kickoff is None and observed is None:
            return None
        # On a live observation without a kick-off time, allow for a match
        # starting on the previous calendar day. Both dates must fit the season.
        earliest = kickoff or observed - timedelta(hours=4)
        latest = kickoff or observed
        seasons = conn.execute('''
            SELECT season, start_date, end_date FROM league_seasons
            WHERE league_id=%s AND start_date <= %s AND end_date >= %s
            LIMIT 2
        ''', (league_id, latest.date(), earliest.date())).fetchall()
        if len(seasons) != 1:
            return None
        season, start, end = seasons[0]
        if start > earliest.date() or end < latest.date():
            return None
    return average_regulation_goals(conn, league_id, season, before or datetime.now(timezone.utc))


def league_goals_for_signal(conn, row, before):
    competition = row.get('league') if row.get('source') == 'fonbet' and row.get('mapping_status') == 'unmatched' else None
    return league_goals(conn, fixture_id=row.get('fixture_id'), league_id=row.get('league_id'),
                        season=row.get('league_season'), fonbet_competition=competition,
                        kickoff_at=row.get('kickoff_at'), observed_at=row.get('observed_at'), before=before)


def league_goals_line(metric):
    if metric is None:
        return 'Среднее голов за матч в лиге: нет данных'
    average = metric['average'].replace('.', ',')
    return f"Среднее голов за матч в лиге: {average} (матчей: {metric['matches']})"
