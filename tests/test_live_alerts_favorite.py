"""Категория favorite: фаворит дома против аутсайдера (specs/онлайн-оповещения.md)."""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from src.jobs import live_alerts as la
from tests.test_collect import conn, make_client  # noqa: F401  (conn — фикстура)
from tests.test_live_alerts import Outbox

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)

LEAGUE, SEASON, TEAMS = 99, 2026, 10


def seed_table(conn, league_type="League", teams=TEAMS, season=SEASON):
    """Команда с меньшим номером всегда побеждает; место в таблице равно номеру."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO leagues (league_id, name, type) VALUES (%s, 'Test League', %s)", (LEAGUE, league_type))
        fid = 1000
        for team in range(1, teams + 1):
            cur.execute("INSERT INTO teams (team_id, name) VALUES (%s, %s)", (team, f"Team {team}"))
        for i in range(1, teams + 1):
            for j in range(1, teams + 1):
                if i == j:
                    continue
                fid += 1
                cur.execute(
                    """INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date, status_short,
                           home_team_id, away_team_id, goals_home, goals_away)
                       VALUES (%s, %s, %s, now(), current_date, 'FT', %s, %s, %s, 0)""",
                    (fid, LEAGUE, season, i, j, 1 if i < j else 0),
                )


def item(fid=1, home=1, away=10, minute=70, goals=(0, 1), status="2H", league_id=LEAGUE):
    return {
        "fixture": {"id": fid, "status": {"short": status, "elapsed": minute}},
        "league": {"id": league_id, "season": SEASON, "name": "Test League", "country": "Testland"},
        "teams": {"home": {"id": home, "name": f"Team {home}"}, "away": {"id": away, "name": f"Team {away}"}},
        "goals": {"home": goals[0], "away": goals[1]},
    }


def run(tmp_path, conn, items, **kwargs):
    client = make_client(tmp_path, [{"response": items}], max_requests=5)
    outbox = Outbox()
    la.run_live_alerts(client, conn, outbox, log=lambda _: None, **kwargs)
    return outbox, client


def stored(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT fixture_id, category, home_rank, away_rank, teams_count FROM live_alerts_sent")
        return cur.fetchall()


def test_favorite_losing_at_home_to_outsider_alerts(tmp_path, conn):
    seed_table(conn)
    outbox, client = run(tmp_path, conn, [item()])
    assert len(outbox.messages) == 1
    text = outbox.messages[0]
    assert "фаворит дома против аутсайдера" in text and "1-е место из 10" in text and "10-е место" in text
    assert stored(conn) == [(1, "favorite", 1, 10, 10)]
    assert client.requests_used == 1  # только live=all


def test_draw_also_alerts_and_win_does_not(tmp_path, conn):
    seed_table(conn)
    outbox, _ = run(tmp_path, conn, [item(1, goals=(1, 1)), item(2, goals=(1, 0)), item(3, goals=(0, 0))])
    assert sorted(r[0] for r in stored(conn)) == [1, 3]
    assert len(outbox.messages) == 2


@pytest.mark.parametrize("home,away", [(3, 10), (1, 8), (2, 9), (10, 1), (5, 6)])
def test_only_top_20_vs_bottom_20_percent(tmp_path, conn, home, away):
    seed_table(conn)
    # 10 команд → по 2 места: фавориты 1–2, аутсайдеры 9–10
    outbox, _ = run(tmp_path, conn, [item(home=home, away=away)])
    assert len(outbox.messages) == (1 if (home, away) == (2, 9) else 0)


@pytest.mark.parametrize("minute,status", [(64, "2H"), (76, "2H"), (70, "ET")])
def test_minute_window_and_status(tmp_path, conn, minute, status):
    seed_table(conn)
    outbox, _ = run(tmp_path, conn, [item(minute=minute, status=status)])
    assert outbox.messages == []


@pytest.mark.parametrize("minute", [65, 75])
def test_window_borders_included(tmp_path, conn, minute):
    seed_table(conn)
    outbox, _ = run(tmp_path, conn, [item(minute=minute)])
    assert len(outbox.messages) == 1


def test_cup_has_no_table(tmp_path, conn):
    seed_table(conn, league_type="Cup")
    outbox, _ = run(tmp_path, conn, [item()])
    assert outbox.messages == []


def test_small_league_and_early_season_are_skipped(tmp_path, conn):
    seed_table(conn, teams=6)  # меньше 8 команд
    outbox, _ = run(tmp_path, conn, [item(away=6)])
    assert outbox.messages == []


def test_one_alert_per_match(tmp_path, conn):
    seed_table(conn)
    client = make_client(tmp_path, [{"response": [item()]}, {"response": [item(minute=72)]}], max_requests=5)
    outbox = Outbox()
    for _ in range(2):
        la.run_live_alerts(client, conn, outbox, log=lambda _: None)
    assert len(outbox.messages) == 1


def test_legacy_categories_are_off_by_default(tmp_path, conn):
    seed_table(conn)
    live = item()
    live["statistics"] = [{"team": {"id": 1}, "statistics": [{"type": "Ball Possession", "value": "70%"},
                                                              {"type": "Shots on Goal", "value": 9}]}]
    outbox, client = run(tmp_path, conn, [live])
    assert [r[1] for r in stored(conn)] == ["favorite"]
    assert client.requests_used == 1


def test_places_rounding():
    assert [la.favorite_places(n) for n in (8, 10, 12, 16, 18, 20)] == [2, 2, 2, 3, 4, 4]
