"""Тесты сбора баскетбольных данных (specs/баскетбол-сбор-данных.md).

Транспорт подменён, настоящих запросов нет (НФТ-8). Разбор проверяется без базы,
запись и очереди — на тестовой базе.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from src.jobs import basketball as bb
from tests.test_collect import conn, make_client  # noqa: F401  (conn — фикстура)

TEST_URL = os.environ.get("TEST_DATABASE_URL")
db = pytest.mark.skipif(not TEST_URL, reason="не задана TEST_DATABASE_URL — тестовая база недоступна")

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- образцы ответов


def game_item(game_id=1, league=12, season="2025-2026", when="2026-01-10T01:00:00+00:00", status="FT",
              home=(1, "Home"), away=(2, "Away"), hs=(25, 20, 30, 25, None, 100), as_=(20, 22, 28, 20, None, 90)):
    def score(v):
        return dict(zip(("quarter_1", "quarter_2", "quarter_3", "quarter_4", "over_time", "total"), v))
    return {
        "id": game_id, "date": when, "stage": None, "week": None, "venue": "Arena",
        "status": {"short": status, "long": "Game Finished"},
        "league": {"id": league, "name": "NBA", "type": "League", "season": season},
        "teams": {"home": {"id": home[0], "name": home[1]}, "away": {"id": away[0], "name": away[1]}},
        "scores": {"home": score(hs), "away": score(as_)},
    }


def team_entry(team_id, fga=80, orb=10):
    return {"game": {"id": 1}, "team": {"id": team_id},
            "field_goals": {"total": 40, "attempts": fga, "percentage": 50},
            "threepoint_goals": {"total": 10, "attempts": 30}, "freethrows_goals": {"total": 15, "attempts": 20},
            "rebounds": {"total": 45, "offence": orb, "defense": None}, "assists": 25, "steals": 7,
            "blocks": 4, "turnovers": 12, "personal_fouls": None}


def player_entry(team_id, player_id, name="P", minutes="32:17", kind="starters"):
    return {"game": {"id": 1}, "team": {"id": team_id}, "player": {"id": player_id, "name": name},
            "type": kind, "minutes": minutes, "points": 14,
            "field_goals": {"total": 5, "attempts": 10}, "threepoint_goals": {"total": 1, "attempts": 3},
            "freethrows_goals": {"total": 3, "attempts": 4}, "rebounds": {"total": 6}, "assists": 2}


def odds_item(bookmakers):
    return {"bookmakers": bookmakers}


def bookmaker(name, bookmaker_id, bets):
    return {"id": bookmaker_id, "name": name, "bets": bets}


def bet(bet_id, name, values):
    return {"id": bet_id, "name": name, "values": [{"value": v, "odd": o} for v, o in values]}


def ladder_values(lines, main):
    values = []
    for line in lines:
        skew = abs(line - main) * 0.1
        values.append((f"Over {line}", f"{1.9 - skew:.2f}"))
        values.append((f"Under {line}", f"{1.9 + skew:.2f}"))
    return values


# ------------------------------------------------------------------ разбор без базы


@pytest.mark.parametrize("value,expected", [("32:17", 1937), ("0:05", 5), ("48:00", 2880), ("", None),
                                              (None, None), ("DNP", None), ("12", 720)])
def test_parse_minutes(value, expected):
    assert bb.parse_minutes(value) == expected


def test_parse_game_with_overtime_and_text_season():
    game = bb.parse_game(game_item(season="2025", status="AOT", hs=(20, 20, 20, 20, 8, 88), as_=(20, 20, 20, 20, 5, 85)))
    assert game["season"] == "2025" and game["status_short"] == "AOT"
    assert (game["home_ot"], game["home_total"], game["away_ot"], game["away_total"]) == (8, 88, 5, 85)
    assert game["game_date"] == date(2026, 1, 10)


def test_parse_game_without_teams_or_time_is_skipped():
    assert bb.parse_game(game_item(home=(None, "X"))) is None
    assert bb.parse_game(game_item(when=None)) is None


def test_parse_team_stats_keeps_missing_fields_null():
    stats = bb.parse_team_stats(team_entry(1, fga=88))
    assert stats["fg_attempts"] == 88 and stats["reb_offence"] == 10
    assert stats["reb_defense"] is None and stats["personal_fouls"] is None  # NULL, не ноль


def test_parse_player_stats_starter_and_minutes():
    stats = bb.parse_player_stats(player_entry(1, 7, minutes="10:35", kind="bench"))
    assert stats["is_starter"] is False and stats["seconds_played"] == 635 and stats["points"] == 14
    assert bb.parse_player_stats({"type": None})["is_starter"] is None


def test_parse_odds_filters_bookmakers_and_markets():
    item = odds_item([
        bookmaker("Pinnacle", 4, [bet(2, "Home/Away", [("Home", "1.5"), ("Away", "2.6")]),
                                  bet(12, "Odd/Even", [("Odd", "1.9")])]),
        bookmaker("Bet365", 8, [bet(4, "Over/Under", ladder_values([220.5], 220.5))]),
    ])
    rows = bb.parse_odds(item)
    assert {(r[1], r[2], r[4]) for r in rows} == {("Pinnacle", 2, "Home"), ("Pinnacle", 2, "Away")}


def test_ladder_keeps_main_line_and_two_neighbours_each_side():
    lines = [215.5 + i for i in range(11)]  # 215.5 .. 225.5
    item = odds_item([bookmaker("1xBet", 5, [bet(4, "Over/Under", ladder_values(lines, 220.5))])])
    rows = bb.parse_odds(item)
    assert sorted({r[5] for r in rows}) == [218.5, 219.5, 220.5, 221.5, 222.5]
    assert {r[4] for r in rows} == {"Over", "Under"}


def test_ladder_near_edge_and_unbalanced_lines():
    item = odds_item([bookmaker("1xBet", 5, [bet(4, "Over/Under", ladder_values([210.5, 211.5, 212.5], 210.5))])])
    assert sorted({r[5] for r in bb.parse_odds(item)}) == [210.5, 211.5, 212.5]
    only_over = odds_item([bookmaker("1xBet", 5, [bet(4, "Over/Under", [("Over 200.5", "1.5"), ("Over 201.5", "1.6")])])])
    assert bb.parse_odds(only_over)  # без пар выбирается средняя линия, ошибки нет


def test_bad_odds_and_values_are_skipped():
    item = odds_item([bookmaker("Pinnacle", 4, [bet(4, "Over/Under", [("Over 200.5", "x"), ("Under 200.5", "1.9"),
                                                                       ("Total", "1.9")])])])
    assert [(r[4], r[5]) for r in bb.parse_odds(item)] == [("Under", 200.5)]


def test_overtime_total_market_is_kept():
    item = odds_item([bookmaker("Pinnacle", 4, [bet(97, "Over/Under (OT)", ladder_values([10.5], 10.5))])])
    assert {r[2] for r in bb.parse_odds(item)} == {97}


# ------------------------------------------------------------------- шаги на базе


def seed_league(conn, league_id=12, season="2025-2026", start="2025-10-01", end="2026-06-30", odds=True):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO basketball.leagues (league_id, name) VALUES (%s, 'NBA') ON CONFLICT DO NOTHING", (league_id,))
        cur.execute("INSERT INTO basketball.league_seasons (league_id, season, start_date, end_date, has_odds) "
                    "VALUES (%s, %s, %s, %s, %s)", (league_id, season, start, end, odds))


def seed_game(conn, game_id, day, status="FT", league=12, season="2025-2026", tip=None):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO basketball.teams (team_id, name) VALUES (1, 'A'), (2, 'B') ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO basketball.games (game_id, league_id, season, tip_off, game_date, status_short, "
                    "home_team_id, away_team_id) VALUES (%s, %s, %s, %s, %s, %s, 1, 2)",
                    (game_id, league, season, tip or f"{day} 20:00+00", day, status))


def count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM basketball.{table}")
        return cur.fetchone()[0]


@db
def test_catalog_writes_leagues_and_coverage_flags_idempotently(tmp_path, conn):
    body = {"response": [{"id": 12, "name": "NBA", "type": "League", "country": {"name": "USA", "code": "US"},
                          "seasons": [{"season": "2025-2026", "start": "2025-10-01", "end": "2026-06-30",
                                       "coverage": {"games": {"statistics": {"teams": True, "players": False}},
                                                    "odds": True, "standings": True}},
                                      {"season": 2019, "coverage": {}}]}]}
    for run in range(2):
        bb.run_catalog(make_client(tmp_path / str(run), [body]), conn)
    assert count(conn, "leagues") == 1 and count(conn, "league_seasons") == 2
    with conn.cursor() as cur:
        cur.execute("SELECT has_team_stats, has_player_stats, has_odds FROM basketball.league_seasons WHERE season = '2025-2026'")
        assert cur.fetchone() == (True, False, True)


@db
def test_games_are_saved_with_teams_and_not_duplicated(tmp_path, conn):
    seed_league(conn)
    body = {"response": [game_item(1), game_item(2, status="AOT", hs=(20, 20, 20, 20, 8, 88)),
                         game_item(3, home=(None, "X"))]}
    first = bb.run_games(make_client(tmp_path / "a", [body]), conn, all_seasons=True, today=date(2026, 10, 20))
    second = bb.run_games(make_client(tmp_path / "b", [body]), conn, all_seasons=True, today=date(2026, 10, 20))
    assert (first.items_processed, first.skipped) == (2, 1) and second.items_processed == 2
    assert count(conn, "games") == 2 and count(conn, "teams") == 2
    with conn.cursor() as cur:
        cur.execute("SELECT home_ot, home_total FROM basketball.games WHERE game_id = 2")
        assert cur.fetchone() == (8, 88)


@db
def test_games_active_selection_skips_old_and_future_seasons(tmp_path, conn):
    seed_league(conn, 12, "2026-2027", "2026-10-01", "2027-06-30")  # идёт сейчас
    seed_league(conn, 13, "2022-2023", "2022-10-01", "2023-06-30")  # закончился давно
    seed_league(conn, 14, "2027-2028", "2027-10-01", "2028-06-30")  # далеко в будущем
    client = make_client(tmp_path, [{"response": []}] * 3)
    bb.run_games(client, conn, today=date(2026, 10, 20))
    assert len(client.transport.calls) == 1 and "league=12" in client.transport.calls[0]


@db
def test_games_since_year_filters_old_seasons(tmp_path, conn):
    seed_league(conn)
    seed_league(conn, 13, "2018-2019", "2018-10-01", "2019-06-30")
    client = make_client(tmp_path, [{"response": []}] * 3)
    bb.run_games(client, conn, since_year=2020, all_seasons=True, today=date(2026, 10, 20))
    assert len(client.transport.calls) == 1


@db
def test_pending_stats_newest_first_finished_only_since_2020(tmp_path, conn):
    seed_league(conn)
    for game_id, day, status in ((1, "2026-09-01", "FT"), (2, "2026-10-10", "AOT"), (3, "2026-10-15", "NS"),
                                 (4, "2019-12-31", "FT"), (5, "2026-10-12", "CANC")):
        seed_game(conn, game_id, day, status)
    assert [g[0] for g in bb.pending_stats(conn, 10)] == [2, 1]


@db
def test_stats_save_team_and_player_rows_and_mark_state(tmp_path, conn):
    seed_league(conn)
    seed_game(conn, 1, "2026-10-10")
    teams = {"response": [team_entry(1), team_entry(2, fga=70, orb=None)]}
    players = {"response": [player_entry(1, 7, "Ann"), player_entry(1, 8, "Bob", "0:00", "bench"), player_entry(2, 9)]}
    client = make_client(tmp_path, [teams, players])

    ctx = bb.run_stats(client, conn)

    assert ctx.items_processed == 1 and client.requests_used == 2
    assert count(conn, "team_game_stats") == 2 and count(conn, "player_game_stats") == 3
    with conn.cursor() as cur:
        cur.execute("SELECT reb_offence FROM basketball.team_game_stats WHERE team_id = 2")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT seconds_played, is_starter FROM basketball.player_game_stats WHERE player_id = 8")
        assert cur.fetchone() == (0, False)
        cur.execute("SELECT team_stats_fetched_at IS NOT NULL, player_stats_fetched_at IS NOT NULL "
                    "FROM basketball.fetch_state WHERE game_id = 1")
        assert cur.fetchone() == (True, True)
    assert bb.pending_stats(conn, 10) == []


@db
def test_empty_response_counts_as_fetched_without_retry(tmp_path, conn):
    seed_league(conn)
    seed_game(conn, 1, "2026-10-10")
    bb.run_stats(make_client(tmp_path, [{"response": []}, {"response": []}]), conn)
    assert count(conn, "team_game_stats") == 0 and bb.pending_stats(conn, 10) == []


@db
def test_failed_requests_give_up_after_three_attempts(tmp_path, conn):
    seed_league(conn)
    seed_game(conn, 1, "2026-10-10")
    failing = {"errors": {"x": "boom"}, "response": []}
    client = make_client(tmp_path, [failing] * 20, max_requests=20)
    bb.run_stats(client, conn)
    with conn.cursor() as cur:
        cur.execute("SELECT team_stats_attempts, player_stats_attempts, team_stats_fetched_at "
                    "FROM basketball.fetch_state WHERE game_id = 1")
        assert cur.fetchone() == (bb.MAX_ATTEMPTS, bb.MAX_ATTEMPTS, None)
    assert client.requests_used == 2 * bb.MAX_ATTEMPTS


@db
def test_stats_stop_at_budget_and_continue_next_run(tmp_path, conn):
    seed_league(conn)
    for i in range(1, 4):
        seed_game(conn, i, f"2026-10-{10 + i}")
    first = make_client(tmp_path / "a", [{"response": []}] * 4, max_requests=4)
    ctx = bb.run_stats(first, conn)
    assert first.requests_used == 4 and ctx.items_processed == 2
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM collection_runs WHERE run_id = %s", (ctx.run_id,))
        assert cur.fetchone()[0] == "quota_exceeded"
    second = make_client(tmp_path / "b", [{"response": []}] * 4, max_requests=4)
    bb.run_stats(second, conn)
    assert bb.pending_stats(conn, 10) == []


@db
def test_stats_rerun_is_idempotent(tmp_path, conn):
    seed_league(conn)
    seed_game(conn, 1, "2026-10-10")
    bb._process_team_stats(make_client(tmp_path / "a", [{"response": [team_entry(1)]}]), conn, 1)
    bb._process_team_stats(make_client(tmp_path / "b", [{"response": [team_entry(1, fga=99)]}]), conn, 1)
    assert count(conn, "team_game_stats") == 1
    with conn.cursor() as cur:
        cur.execute("SELECT fg_attempts FROM basketball.team_game_stats")
        assert cur.fetchone()[0] == 99


@db
def test_odds_snapshot_for_upcoming_games_only_with_coverage(tmp_path, conn):
    seed_league(conn)
    seed_league(conn, 20, "2025-2026", odds=False)
    seed_game(conn, 1, "2026-10-21", status="NS", tip=NOW + timedelta(hours=10))                       # берём
    seed_game(conn, 2, "2026-10-25", status="NS", tip=NOW + timedelta(hours=100))                      # далеко
    seed_game(conn, 3, "2026-10-19", status="FT", tip=NOW - timedelta(hours=20))                       # уже сыграна
    seed_game(conn, 4, "2026-10-21", status="NS", league=20, tip=NOW + timedelta(hours=10))            # нет покрытия
    body = {"response": [odds_item([
        bookmaker("Pinnacle", 4, [bet(2, "Home/Away", [("Home", "1.5"), ("Away", "2.6")]),
                                  bet(4, "Over/Under", ladder_values([218.5 + i for i in range(9)], 222.5))]),
        bookmaker("1xBet", 5, [bet(5, "Over/Under 1st Half", ladder_values([110.5], 110.5))]),
    ])]}
    client = make_client(tmp_path, [body])

    ctx = bb.run_odds(client, conn, now=NOW)

    assert ctx.items_processed == 1 and client.requests_used == 1
    assert count(conn, "odds_snapshots") == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT line) FROM basketball.odds_values WHERE bet_type_id = 4")
        assert cur.fetchone()[0] == 5
        cur.execute("SELECT count(*) FROM basketball.odds_values WHERE bet_type_id = 2 AND line = 0")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT name FROM basketball.bookmakers ORDER BY name")
        assert [r[0] for r in cur.fetchall()] == ["1xBet", "Pinnacle"]


@db
def test_odds_without_target_bookmakers_make_no_snapshot(tmp_path, conn):
    seed_league(conn)
    seed_game(conn, 1, "2026-10-21", status="NS", tip=NOW + timedelta(hours=5))
    body = {"response": [odds_item([bookmaker("Bet365", 8, [bet(2, "Home/Away", [("Home", "1.5")])])])]}
    ctx = bb.run_odds(make_client(tmp_path, [body]), conn, now=NOW)
    assert ctx.skipped == 1 and count(conn, "odds_snapshots") == 0


@db
def test_two_snapshots_keep_history(tmp_path, conn):
    seed_league(conn)
    seed_game(conn, 1, "2026-10-21", status="NS", tip=NOW + timedelta(hours=20))
    body = {"response": [odds_item([bookmaker("Pinnacle", 4, [bet(2, "Home/Away", [("Home", "1.5"), ("Away", "2.6")])])])]}
    bb.run_odds(make_client(tmp_path / "a", [body]), conn, now=NOW)
    bb.run_odds(make_client(tmp_path / "b", [body]), conn, now=NOW + timedelta(hours=12))
    assert count(conn, "odds_snapshots") == 2


@db
def test_report_counts_by_month(tmp_path, conn, capsys):
    seed_league(conn)
    seed_game(conn, 1, "2026-10-10")
    seed_game(conn, 2, "2026-10-11")
    bb.run_stats(make_client(tmp_path, [{"response": [team_entry(1)]}, {"response": [player_entry(1, 7)]}], max_requests=2), conn)
    rows = bb.completeness_report(conn)
    assert rows[0][1] == 2 and rows[0][2] == 1 and rows[0][3] == 1 and rows[0][4] == 1
    bb.print_report(conn)
    assert "месяц" in capsys.readouterr().out
