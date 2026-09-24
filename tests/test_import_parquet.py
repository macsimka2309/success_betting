"""Тесты переноса из parquet.

Работают на маленьких искусственных наборах и временной базе
(TEST_DATABASE_URL). Production не затрагивается (НФТ-8).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from src.common.keys import event_key
from src.db import migrate
from src.jobs import import_parquet as imp

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


# ------------------------------------------------------------ вспомогательное


def write_parquet(path, rows: list[dict]) -> None:
    pq.write_table(pa.Table.from_pylist(rows), path)


@pytest.fixture()
def source(tmp_path):
    """Маленький, но полный набор: лига, два матча, события, статистика."""
    mapping = {
        "E0": {
            "id": 39,
            "name": "Premier League",
            "country": "England",
            "seasons": [2025, 2026],
            "coverage": {
                "events": True,
                "statistics_fixtures": True,
                "lineups": True,
                "injuries": False,
            },
        }
    }
    (tmp_path / "apifootball_leagues.json").write_text(
        json.dumps(mapping), encoding="utf-8"
    )

    write_parquet(
        tmp_path / "leagues_catalog.parquet",
        [
            {
                "id": 39,
                "name": "Premier League",
                "type": "League",
                "country": "England",
                "cc": "GB",
            }
        ],
    )
    write_parquet(
        tmp_path / "fixtures.parquet",
        [
            {
                "fixture_id": 1001,
                "LeagueCode": "E0",
                "Country": "England",
                "Season": 2025,
                "Date": "2025-08-15",
                "kickoff": "2025-08-15T19:00:00+00:00",
                "HomeTeam": "Arsenal",
                "AwayTeam": "Chelsea",
                "home_id": 42,
                "away_id": 49,
                "FTHG": 2.0,
                "FTAG": 1.0,
                "status": "FT",
            },
            {
                "fixture_id": 1002,
                "LeagueCode": "E0",
                "Country": "England",
                "Season": 2025,
                "Date": "2025-08-16",
                "kickoff": "2025-08-16T14:00:00+00:00",
                "HomeTeam": "Fulham",
                "AwayTeam": "Arsenal",
                "home_id": 36,
                "away_id": 42,
                "FTHG": None,
                "FTAG": None,
                "status": "NS",
            },
        ],
    )
    write_parquet(
        tmp_path / "events.parquet",
        [
            {
                "fixture_id": 1001,
                "minute": 11,
                "minute_extra": None,
                "team": "Arsenal",
                "team_id": 42.0,
                "type": "Goal",
                "detail": "Normal Goal",
                "player": "B. Saka",
                "player_id": 1460.0,
                "assist": "M. Ødegaard",
                "comments": None,
            },
            {
                "fixture_id": 9999,  # матча нет — строка должна быть пропущена
                "minute": 5,
                "minute_extra": None,
                "team": "Ghost",
                "team_id": 1.0,
                "type": "Goal",
                "detail": "Normal Goal",
                "player": "Nobody",
                "player_id": 2.0,
                "assist": None,
                "comments": None,
            },
        ],
    )
    write_parquet(
        tmp_path / "statistics.parquet",
        [
            {
                "fixture_id": 1001,
                "team_id": 42,
                "team": "Arsenal",
                "ball_possession": "58%",
                "passes_pct": "84%",
                "expected_goals": "1.74",
                "goals_prevented": None,
                "total_shots": 14,
                "corner_kicks": 7,
            },
            {
                "fixture_id": 1001,
                "team_id": 49,
                "team": "Chelsea",
                "ball_possession": "нет данных",  # нечисловое значение
                "passes_pct": None,
                "expected_goals": "0.93",
                "goals_prevented": None,
                "total_shots": 9,
                "corner_kicks": 3,
            },
        ],
    )
    return tmp_path


@pytest.fixture()
def conn():
    with psycopg.connect(TEST_URL) as connection:
        with connection.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        connection.commit()
        migrate.run_up(connection)
        yield connection


def count(connection, table: str) -> int:
    with connection.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def run_all(connection, source_dir, dry_run: bool = False) -> None:
    for step in (
        imp.import_leagues,
        imp.import_fixtures,
        imp.import_events,
        imp.import_statistics,
    ):
        step(connection, source_dir, None, dry_run)
    imp.fill_fetch_state(connection, dry_run)


# ------------------------------------------------------ преобразование типов


def test_percent_parsing():
    assert imp.to_percent("58%") == 58
    assert imp.to_percent("нет данных") is None
    assert imp.to_percent(None) is None


def test_decimal_and_int_parsing():
    assert imp.to_decimal("1.74") == 1.74
    assert imp.to_int(867.0) == 867
    assert imp.to_int(float("nan")) is None


def test_kickoff_converted_to_utc():
    moment = imp.to_utc("2025-08-15T21:00:00+02:00")
    assert moment == datetime(2025, 8, 15, 19, 0, tzinfo=timezone.utc)


def test_event_key_same_for_parquet_and_api():
    """Ключ обязан совпадать для обоих путей записи, иначе события задвоятся."""
    from_parquet = event_key(1001, 11, float("nan"), 42.0, "Goal", "Normal Goal", 1460.0)
    from_api = event_key(1001, 11, None, 42, "Goal", "Normal Goal", 1460)
    assert from_parquet == from_api
    assert from_parquet == "1001|11||42|Goal|Normal Goal|1460"


# --------------------------------------------------------------- перенос


def test_import_writes_expected_rows(conn, source):
    run_all(conn, source)
    assert count(conn, "leagues") == 1
    assert count(conn, "league_seasons") == 2
    assert count(conn, "teams") == 3  # из матчей; события и статистика те же
    assert count(conn, "fixtures") == 2
    assert count(conn, "players") == 1
    assert count(conn, "fixture_events") == 1
    assert count(conn, "fixture_statistics") == 2


def test_mapped_leagues_are_marked_tracked(conn, source):
    """Наши лиги (есть прежний код) входят в охват сбора, остальные — нет (ФТ-9)."""
    write_parquet(
        source / "leagues_catalog.parquet",
        [
            {"id": 39, "name": "Premier League", "type": "League", "country": "England", "cc": "GB"},
            {"id": 999, "name": "Cup Not Ours", "type": "Cup", "country": "World", "cc": None},
        ],
    )
    imp.import_leagues(conn, source, None, False)
    with conn.cursor() as cur:
        cur.execute("SELECT league_id, is_tracked FROM leagues ORDER BY league_id")
        assert cur.fetchall() == [(39, True), (999, False)]


def test_import_is_idempotent(conn, source):
    run_all(conn, source)
    before = {t: count(conn, t) for t in ("fixtures", "fixture_events", "teams")}
    run_all(conn, source)
    after = {t: count(conn, t) for t in ("fixtures", "fixture_events", "teams")}
    assert before == after


def test_team_only_in_events_is_added_to_reference(conn, source):
    """В fixtures.parquet есть не все команды из событий (реальный случай)."""
    write_parquet(
        source / "events.parquet",
        [
            {
                "fixture_id": 1001,
                "minute": 30,
                "minute_extra": None,
                "team": "Неизвестная",
                "team_id": 864.0,  # этой команды нет в fixtures.parquet
                "type": "Card",
                "detail": "Yellow Card",
                "player": "X. Y.",
                "player_id": 77.0,
                "assist": None,
                "comments": None,
            }
        ],
    )
    imp.import_leagues(conn, source, None, False)
    imp.import_fixtures(conn, source, None, False)
    imp.import_events(conn, source, None, False)
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM teams WHERE team_id = 864")
        assert cur.fetchone() == ("Неизвестная",)
    assert count(conn, "fixture_events") == 1


def test_event_without_fixture_is_skipped(conn, source):
    reports = imp.import_leagues(conn, source, None, False)
    reports += imp.import_fixtures(conn, source, None, False)
    reports += imp.import_events(conn, source, None, False)
    events_report = next(r for r in reports if r.name == "события")
    assert events_report.written == 1
    assert events_report.skipped["матч отсутствует в базе"] == 1


def test_non_numeric_percent_becomes_null(conn, source):
    run_all(conn, source)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ball_possession, expected_goals FROM fixture_statistics "
            "WHERE fixture_id = 1001 AND team_id = 49"
        )
        possession, xg = cur.fetchone()
    assert possession is None
    assert float(xg) == 0.93


def test_missing_fields_are_null_not_zero(conn, source):
    """Несыгранный матч: счёт пуст, а не ноль."""
    run_all(conn, source)
    with conn.cursor() as cur:
        cur.execute("SELECT goals_home, goals_away, round FROM fixtures WHERE fixture_id = 1002")
        assert cur.fetchone() == (None, None, None)


def test_fetch_state_marks_only_collected_sets(conn, source):
    run_all(conn, source)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT events_fetched_at IS NOT NULL, statistics_fetched_at IS NOT NULL, "
            "lineups_fetched_at IS NOT NULL FROM fixture_fetch_state WHERE fixture_id = 1001"
        )
        assert cur.fetchone() == (True, True, False)
        cur.execute(
            "SELECT events_fetched_at, statistics_fetched_at "
            "FROM fixture_fetch_state WHERE fixture_id = 1002"
        )
        assert cur.fetchone() == (None, None)


def test_batches_are_committed_during_import(source):
    """Пакеты фиксируются по ходу, а не одной транзакцией в конце.

    Иначе обрыв переноса теряет всю работу (ПР-8). Проверяем тем, что
    записанное видно ДРУГОМУ соединению, пока переносящее ещё открыто.
    """
    from src.db.connection import connect as open_connection

    with psycopg.connect(TEST_URL) as setup:
        with setup.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        setup.commit()
        migrate.run_up(setup)

    with open_connection(TEST_URL) as importing:
        imp.import_leagues(importing, source, None, False)
        imp.import_fixtures(importing, source, None, False)
        imp.import_events(importing, source, None, False)
        with psycopg.connect(TEST_URL) as observer:
            assert count(observer, "fixtures") == 2
            assert count(observer, "fixture_events") == 1


def test_dry_run_writes_nothing(conn, source):
    run_all(conn, source, dry_run=True)
    assert count(conn, "fixtures") == 0
    assert count(conn, "fixture_events") == 0


def test_missing_file_does_not_stop_import(conn, source, tmp_path):
    (source / "statistics.parquet").unlink()
    run_all(conn, source)
    assert count(conn, "fixtures") == 2
    assert count(conn, "fixture_statistics") == 0
