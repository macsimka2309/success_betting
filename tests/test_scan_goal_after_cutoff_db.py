"""Интеграционные тесты загрузчиков scan_goal_after_cutoff (нужна БД).

Временная база (TEST_DATABASE_URL), как и остальные DB-тесты проекта.
Проверяет именно SQL-склейку (РА-1/РА-2), которую синтетические тесты
(test_scan_goal_after_cutoff.py) не покрывают, поскольку работают с
уже готовым DataFrame, а не с запросами.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.test_collect import conn  # noqa: F401  (conn — фикстура)

from src.jobs.scan_goal_after_cutoff import load_base_sample, load_goal_events, load_substitution_events

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


def seed_fixture(conn, fixture_id, home_poss, away_poss, home_shots, away_shots,
                  home_sog, away_sog, goals_home, goals_away, status="FT",
                  create_fetch_state=True):
    now_utc = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leagues (league_id, name) VALUES (1, 'L') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO teams (team_id, name) VALUES (100, 'Home'), (200, 'Away') "
            "ON CONFLICT DO NOTHING"
        )
        cur.execute(
            """INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date,
               status_short, home_team_id, away_team_id, goals_home, goals_away)
               VALUES (%s, 1, 2023, %s, %s, %s, 100, 200, %s, %s)""",
            (fixture_id, now_utc, now_utc.date(), status, goals_home, goals_away),
        )
        cur.execute(
            "INSERT INTO fixture_statistics (fixture_id, team_id, ball_possession, total_shots, shots_on_goal) "
            "VALUES (%s, 100, %s, %s, %s)",
            (fixture_id, home_poss, home_shots, home_sog),
        )
        cur.execute(
            "INSERT INTO fixture_statistics (fixture_id, team_id, ball_possession, total_shots, shots_on_goal) "
            "VALUES (%s, 200, %s, %s, %s)",
            (fixture_id, away_poss, away_shots, away_sog),
        )
        if create_fetch_state:
            cur.execute(
                "INSERT INTO fixture_fetch_state (fixture_id, events_fetched_at) VALUES (%s, now())",
                (fixture_id,),
            )


def test_load_base_sample_includes_fixture_without_fetch_state_row(conn):
    """Регрессия: LEFT JOIN, не JOIN — иначе матчи без строки в
    fixture_fetch_state тихо пропадают из выборки целиком (РА-1), вместо
    того чтобы попасть туда с events_fetched=False (как для РА-2)."""
    seed_fixture(conn, 1, 65, 35, 10, 6, 5, 2, 1, 0, create_fetch_state=False)

    df = load_base_sample(conn)

    assert len(df) == 1
    assert df.iloc[0]["fixture_id"] == 1
    assert bool(df.iloc[0]["events_fetched"]) is False


def test_load_base_sample_reflects_events_fetched_flag(conn):
    seed_fixture(conn, 2, 60, 40, 8, 5, 4, 1, 2, 1, create_fetch_state=True)

    df = load_base_sample(conn)

    assert bool(df.iloc[0]["events_fetched"]) is True
    assert df.iloc[0]["home_possession"] == 60
    assert df.iloc[0]["home_total_shots"] == 8


def test_load_base_sample_excludes_rows_missing_statistics(conn):
    now_utc = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO leagues (league_id, name) VALUES (1, 'L') ON CONFLICT DO NOTHING")
        cur.execute(
            "INSERT INTO teams (team_id, name) VALUES (100, 'Home'), (200, 'Away') "
            "ON CONFLICT DO NOTHING"
        )
        cur.execute(
            """INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date,
               status_short, home_team_id, away_team_id, goals_home, goals_away)
               VALUES (3, 1, 2023, %s, %s, 'FT', 100, 200, 1, 0)""",
            (now_utc, now_utc.date()),
        )
        # нет строк fixture_statistics вообще — матч без статистики

    df = load_base_sample(conn)
    assert df.empty


def test_load_goal_events_filters_type_and_missed_penalty(conn):
    seed_fixture(conn, 4, 60, 40, 8, 5, 4, 1, 1, 0, create_fetch_state=True)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fixture_events (fixture_id, team_id, minute, type, detail, event_key) VALUES "
            "(4, 100, 50, 'Goal', 'Normal Goal', 'k1'), "
            "(4, 200, 60, 'Card', 'Yellow Card', 'k2'), "
            "(4, 100, 70, 'Goal', 'Missed Penalty', 'k3')"
        )

    df = load_goal_events(conn, [4])
    assert len(df) == 1
    assert df.iloc[0]["minute"] == 50


def test_load_substitution_events_returns_subst_type_with_team(conn):
    seed_fixture(conn, 5, 60, 40, 8, 5, 4, 1, 1, 0, create_fetch_state=True)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fixture_events (fixture_id, team_id, minute, type, detail, event_key) VALUES "
            "(5, 100, 55, 'subst', NULL, 'k1'), "
            "(5, 200, 70, 'subst', NULL, 'k2'), "
            "(5, 100, 80, 'Card', 'Yellow Card', 'k3')"
        )

    subst = load_substitution_events(conn, [5])
    assert len(subst) == 2  # Card не попадает — только type='subst'
    assert set(subst["team_id"]) == {100, 200}
