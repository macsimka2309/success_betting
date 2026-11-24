import os
from datetime import timedelta

import pytest

from src.jobs.league_goals import league_goals
from src.jobs.live_alerts import run_live_alerts
from src.jobs.signal_reminders import dispatch_reminders
from src.jobs.signal_tracking import configure
from tests.test_alert_dispatch import NOW, api, fb, link, run
from tests.test_collect import conn, make_client  # noqa: F401
from tests.test_live_alerts import live_item
from tests.test_signal_reminders import fresh_fonbet

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='disposable database required')


def add_result(conn, fixture_id, *, score, regular=(None, None), status='FT', season=2026, future=False):
    kickoff = NOW + timedelta(days=1 if future else -3)
    conn.execute('''
        INSERT INTO fixtures(fixture_id,league_id,season,kickoff_at,match_date,status_short,
                             home_team_id,away_team_id,goals_home,goals_away,ft90_home,ft90_away)
        VALUES(%s,3,%s,%s,%s,%s,1,2,%s,%s,%s,%s)
    ''', (fixture_id, season, kickoff, kickoff.date(), status, *score, *regular))


def test_league_mean_uses_regulation_results_in_one_season(conn):
    api(conn)
    add_result(conn, 11, score=(2, 0))
    add_result(conn, 12, score=(4, 3), regular=(1, 1), status='AET')
    add_result(conn, 13, score=(7, 6), regular=(0, 0), status='PEN')
    add_result(conn, 14, score=(4, 3), status='PEN')
    add_result(conn, 15, score=(3, 0), status='AWD')
    add_result(conn, 16, score=(4, None))
    add_result(conn, 17, score=(8, 0), season=2025)
    add_result(conn, 18, score=(9, 0), future=True)
    assert league_goals(conn, fixture_id=10, before=NOW) == {'average': '1.33', 'matches': 3, 'season': 2026}
    assert league_goals(conn, fixture_id=10, season=2025, before=NOW) is None


def test_approved_fonbet_alias_needs_one_matching_season(conn):
    api(conn)
    add_result(conn, 11, score=(2, 0))
    conn.execute("INSERT INTO fonbet_competition_aliases(alias_key,league_id,note) VALUES('youth league',3,'checked')")
    conn.execute("INSERT INTO league_seasons(league_id,season,start_date,end_date) VALUES (3,2026,'2026-01-01','2026-12-31')")
    def measure(name):
        return league_goals(conn, fonbet_competition=name, kickoff_at=NOW, before=NOW)
    assert measure('Youth League Women') is None
    assert measure('Youth, League') == {'average': '2.00', 'matches': 1, 'season': 2026}
    assert league_goals(conn, fonbet_competition='Youth League', observed_at=NOW, before=NOW) == measure('Youth League')
    season_boundary = NOW.replace(month=1, day=1, hour=1)
    assert league_goals(conn, fonbet_competition='Youth League', observed_at=season_boundary, before=NOW) is None
    conn.execute("INSERT INTO league_seasons(league_id,season,start_date,end_date) VALUES (3,2027,'2026-11-01','2027-10-31')")
    assert measure('Youth League') is None


@pytest.mark.parametrize('source', ['api_football', 'fonbet', 'fonbet_unlinked'])
def test_first_message_sends_for_low_or_unknown_average(conn, source):
    if source == 'api_football':
        api(conn)
    else:
        api(conn, 4, None)
    add_result(conn, 11, score=(1, 0))
    add_result(conn, 12, score=(2, 0))
    if source != 'api_football':
        fb(conn)
        if source == 'fonbet':
            link(conn)
    sent = []
    assert run(conn, lambda text, markup: sent.append(text) or 101)['sent'] == 1
    expected = 'нет данных' if source == 'fonbet_unlinked' else '1,50 (матчей: 2)'
    assert f'Среднее голов за матч в лиге: {expected}' in sent[0]


def test_reminder_displays_the_same_average_as_first_message(conn):
    configure(conn, tracking=True, reminders=True, now=NOW)
    api(conn, 4, None)
    add_result(conn, 11, score=(1, 0))
    fb(conn)
    link(conn)
    sent = []
    assert run(conn, lambda text, markup: sent.append(text) or 101)['sent'] == 1
    add_result(conn, 12, score=(7, 0))
    at = NOW + timedelta(seconds=10)
    fresh_fonbet(conn, at=at)
    assert dispatch_reminders(conn, lambda text, markup: sent.append(text) or 102,
                              now_fn=lambda: at) == {'reminder_sent': 1}
    assert all('Среднее голов за матч в лиге: 1,00 (матчей: 1)' in text for text in sent)


def test_direct_api_delivery_displays_known_mean(conn, tmp_path):
    api(conn)
    add_result(conn, 11, score=(1, 1))
    match = live_item(10, minute=70)
    match['league'].update(id=3, season=2026)
    match['teams']['home']['id'] = 1
    match['teams']['away']['id'] = 2
    statistics = {'response': [{'team': {'id': team}, 'statistics_2h': [
        {'type': 'Shots on Goal', 'value': count},
    ]} for team, count in [(1, 6), (2, 0)]]}
    client = make_client(tmp_path, [{'response': [match]}, statistics])
    sent = []
    run_live_alerts(client, conn, lambda text, markup: sent.append(text))
    assert len(sent) == 1
    assert 'Среднее голов за матч в лиге: 2,00 (матчей: 1)' in sent[0]
