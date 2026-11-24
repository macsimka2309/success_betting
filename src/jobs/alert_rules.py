"""Network-free shared half-pressure trigger and source selection."""
import math
import re
from datetime import datetime, timedelta, timezone

TTL = timedelta(seconds=120)


def shots(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if math.isfinite(value) and value >= 0 and int(value) == value else None


def pressure(home, away):
    """True/False/None (missing data); never treat a missing team as zero."""
    home, away = shots(home), shots(away)
    if any(v is not None and v >= 6 for v in (home, away)):
        return True
    if home is None or away is None:
        return None
    return home + away >= 8


def excluded(competition, tags=()):
    competition = (competition or '').strip()
    text = ' '.join([competition, *map(str, tags or ())])
    return (not competition or competition.casefold().startswith('fc') or
            bool(re.search(r'футзал|futsal|микрофутзал|кибер|esports|h2h', competition, re.I)) or
            bool(re.search(r'(?<!\d)2\s*[xх×]\s*40(?!\d)', text, re.I)))


def fresh(value, now):
    return isinstance(value, datetime) and value.tzinfo is not None and now - TTL <= value <= now


def usable(row, now):
    if not fresh(row.get('observed_at'), now) or not fresh(row.get('context_at'), now):
        return False
    if not row.get('present') or row.get('status') != '2H' or not 65 <= (row.get('current_minute') or 0) <= 75:
        return False
    if not 65 <= (row.get('elapsed') or 0) <= 75 or excluded(row.get('league'), row.get('format_tags')):
        return False
    return pressure(row.get('home_shots'), row.get('away_shots')) is not None


def choose(rows, now):
    valid = [r for r in rows if usable(r, now)]
    api = [r for r in valid if r['source'] == 'api_football']
    if api:
        return max(api, key=lambda r: (r['elapsed'], r['observed_at'], r['event_id']))
    fonbet = [r for r in valid if r['source'] == 'fonbet']
    return max(fonbet, key=lambda r: (r['elapsed'], r['observed_at'], r['event_id'])) if fonbet else None
