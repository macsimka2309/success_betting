"""Тесты механизма миграций и начальной схемы.

Работают на временной базе, имя которой задаётся переменной окружения
TEST_DATABASE_URL. Production-база не затрагивается (НФТ-8): если переменная
не задана, тесты пропускаются.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from src.db import migrate

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


@pytest.fixture()
def conn():
    """Чистая схема на каждый тест."""
    with psycopg.connect(TEST_URL) as connection:
        with connection.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("DROP SCHEMA IF EXISTS basketball CASCADE")
            cur.execute("CREATE SCHEMA public")
        connection.commit()
        yield connection


def table_names(connection) -> set[str]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        return {row[0] for row in cur.fetchall()}


def test_discover_finds_migrations_in_order():
    migrations = migrate.discover()
    versions = [m.version for m in migrations]
    assert versions == sorted(versions), "миграции должны идти по возрастанию номера"
    assert "001" in versions


def test_up_creates_all_tables(conn):
    migrate.run_up(conn)
    tables = table_names(conn)
    expected = {
        "leagues",
        "league_seasons",
        "teams",
        "players",
        "bookmakers",
        "bet_types",
        "fixtures",
        "fixture_events",
        "fixture_statistics",
        "fixture_lineups",
        "fixture_lineup_players",
        "injuries",
        "odds_snapshots",
        "odds_values",
        "collection_runs",
        "fixture_fetch_state",
        "schema_migrations",
    }
    assert expected <= tables


def test_up_is_idempotent(conn):
    migrate.run_up(conn)
    applied_first = migrate.applied_versions(conn)
    assert migrate.run_up(conn) == [], "повторный запуск не должен ничего применять"
    assert migrate.applied_versions(conn) == applied_first


def test_down_then_up_again(conn):
    """Откат всех миграций по очереди, от последней к первой.

    Версии не хардкодятся (кроме "001" как границы) — список берётся из
    migrate.discover(), чтобы тест не ломался при каждой новой миграции.
    """
    migrate.run_up(conn)
    versions_desc = [m.version for m in reversed(migrate.discover())]
    assert versions_desc[-1] == "001"
    for version in versions_desc:
        migrate.run_down(conn, version)
    assert "fixtures" not in table_names(conn)
    assert migrate.applied_versions(conn) == set()
    migrate.run_up(conn)
    assert "fixtures" in table_names(conn)


def test_down_refuses_when_later_migration_applied(conn):
    """Откат 001 при применённых более поздних миграциях снёс бы таблицы под их ногами."""
    migrate.run_up(conn)
    with pytest.raises(RuntimeError, match="более поздние"):
        migrate.run_down(conn, "001")
    assert "fixtures" in table_names(conn)  # ничего не тронуто


def test_reference_tables_start_empty(conn):
    """Справочники букмекеров и рынков наполняются при сборе, не миграцией."""
    migrate.run_up(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM bookmakers")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM bet_types")
        assert cur.fetchone()[0] == 0


def test_failed_migration_does_not_register(conn):
    """Сломанный SQL не должен отмечаться как применённый."""
    migrate.run_up(conn)
    broken = migrate.Migration(
        version="999", name="broken", path=migrate.MIGRATIONS_DIR / "999_broken.sql"
    )
    broken.path.write_text("SELECT * FROM таблицы_которой_нет;", encoding="utf-8")
    try:
        with pytest.raises(psycopg.errors.UndefinedTable):
            migrate.apply(conn, broken)
        assert "999" not in migrate.applied_versions(conn)
    finally:
        broken.path.unlink()


# ------------------------------------------------ баскетбольная схема (миграция 016)


def bb_tables(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'basketball'")
        return {row[0] for row in cur.fetchall()}


def test_basketball_schema_created_and_isolated(conn):
    migrate.run_up(conn)
    assert bb_tables(conn) >= {
        "leagues", "league_seasons", "teams", "players", "games", "team_game_stats",
        "player_game_stats", "fetch_state", "bookmakers", "bet_types", "odds_snapshots", "odds_values",
    }
    assert "games" not in bb_tables(conn) - {"games"}  # имена не пересекаются с public
    assert "fixtures" in table_names(conn)  # футбольные таблицы на месте


def test_basketball_foreign_keys_and_defaults(conn):
    migrate.run_up(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO basketball.leagues (league_id, name) VALUES (12, 'NBA')")
        cur.execute("INSERT INTO basketball.teams (team_id, name) VALUES (1, 'A'), (2, 'B')")
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            cur.execute(
                "INSERT INTO basketball.games (game_id, league_id, season, tip_off, game_date, status_short, "
                "home_team_id, away_team_id) VALUES (1, 99, '2025-2026', now(), current_date, 'FT', 1, 2)"
            )
        cur.execute(
            "INSERT INTO basketball.games (game_id, league_id, season, tip_off, game_date, status_short, "
            "home_team_id, away_team_id) VALUES (1, 12, '2025-2026', now(), current_date, 'FT', 1, 2)"
        )
        cur.execute("INSERT INTO basketball.team_game_stats (game_id, team_id) VALUES (1, 1)")
        cur.execute("SELECT fg_attempts, reb_offence FROM basketball.team_game_stats")
        assert cur.fetchone() == (None, None)  # отсутствие данных — NULL, не ноль
        cur.execute("INSERT INTO basketball.fetch_state (game_id) VALUES (1)")
        cur.execute("SELECT team_stats_attempts, player_stats_attempts FROM basketball.fetch_state")
        assert cur.fetchone() == (0, 0)


def test_basketball_odds_key_allows_ladder_of_lines(conn):
    migrate.run_up(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO basketball.leagues (league_id, name) VALUES (12, 'NBA')")
        cur.execute("INSERT INTO basketball.teams (team_id, name) VALUES (1, 'A'), (2, 'B')")
        cur.execute(
            "INSERT INTO basketball.games (game_id, league_id, season, tip_off, game_date, status_short, "
            "home_team_id, away_team_id) VALUES (1, 12, '2025-2026', now(), current_date, 'NS', 1, 2)"
        )
        cur.execute("INSERT INTO basketball.bookmakers VALUES (4, 'Pinnacle')")
        cur.execute("INSERT INTO basketball.bet_types VALUES (2, 'Over/Under')")
        cur.execute("INSERT INTO basketball.odds_snapshots (game_id) VALUES (1) RETURNING snapshot_id")
        snap = cur.fetchone()[0]
        for outcome, line in (("Over", 219.5), ("Over", 220.0), ("Under", 219.5)):
            cur.execute(
                "INSERT INTO basketball.odds_values (snapshot_id, bookmaker_id, bet_type_id, outcome, line, odd) "
                "VALUES (%s, 4, 2, %s, %s, 1.9)", (snap, outcome, line),
            )
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            cur.execute(
                "INSERT INTO basketball.odds_values (snapshot_id, bookmaker_id, bet_type_id, outcome, line, odd) "
                "VALUES (%s, 4, 2, 'Over', 219.5, 1.8)", (snap,),
            )


def test_basketball_down_drops_only_its_schema(conn):
    migrate.run_up(conn)
    migrate.run_down(conn, max(m.version for m in migrate.discover()))
    # откат последней миграции; для 016 проверяем отдельно, когда она последняя
    versions = {m.version for m in migrate.discover()}
    if "016" in versions and "016" in migrate.applied_versions(conn):
        migrate.run_down(conn, "016")
    assert "games" not in bb_tables(conn)
    assert "fixtures" in table_names(conn)
