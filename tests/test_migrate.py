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
    migrate.run_up(conn)
    migrate.run_down(conn, "002")
    migrate.run_down(conn, "001")
    assert "fixtures" not in table_names(conn)
    assert migrate.applied_versions(conn) == set()
    migrate.run_up(conn)
    assert "fixtures" in table_names(conn)


def test_down_refuses_when_later_migration_applied(conn):
    """Откат 001 при применённой 002 снёс бы таблицы под её ногами."""
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
