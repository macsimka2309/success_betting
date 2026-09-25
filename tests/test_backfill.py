"""Тесты дозагрузки пропущенного (specs/дозагрузка-пропущенного.md).

Подменённый транспорт и временная база, без сети (НФТ-8).
"""

from __future__ import annotations

import os
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

from src.jobs import collect
from tests.test_collect import conn, make_client  # noqa: F401  (conn — фикстура)

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)

TODAY = date(2026, 9, 25)
SINCE = date(2026, 7, 8)
EMPTY = {"response": []}


def seed(conn, matches, tracked=True, coverage=(True, True, True), season=2020):
    """matches — список (fixture_id, дата матча). Сезон 2020 нарочно старый:
    шаги «матчи» и «травмы» его не берут и не тратят бюджет."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leagues (league_id, name, is_tracked) VALUES (39, 'PL', %s)", (tracked,)
        )
        cur.execute(
            "INSERT INTO league_seasons (league_id, season, has_events, has_statistics, "
            "has_lineups, has_injuries) VALUES (39, %s, %s, %s, %s, true)",
            (season, *coverage),
        )
        cur.execute("INSERT INTO teams (team_id, name) VALUES (36, 'A'), (34, 'B')")
        for fixture_id, day in matches:
            cur.execute(
                "INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date, "
                "status_short, home_team_id, away_team_id) "
                "VALUES (%s, 39, %s, %s, %s, 'FT', 36, 34)",
                (fixture_id, season, f"{day} 15:00+00", day),
            )


def calls(client):
    """[(набор, fixture_id)] в порядке запросов."""
    result = []
    for url in client.transport.calls:
        endpoint, _, query = url.partition("?")
        result.append((endpoint.rsplit("/", 1)[-1], int(query.split("fixture=")[1].split("&")[0])))
    return result


# ----------------------------------------------------------- окно свежих (ДЗ-1)


def test_fresh_step_takes_only_recent_matches(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 24)), (2, date(2026, 9, 10))])
    client = make_client(tmp_path, [EMPTY])

    collect.run_events(client, conn, today=TODAY)

    assert calls(client) == [("events", 1)]


def test_fresh_window_boundary_is_inclusive(tmp_path, conn):
    """Ровно fresh_days назад — ещё свежий; на день раньше — уже нет."""
    seed(conn, [(1, date(2026, 9, 22)), (2, date(2026, 9, 21))])
    client = make_client(tmp_path, [EMPTY])

    collect.run_events(client, conn, today=TODAY, fresh_days=3)

    assert calls(client) == [("events", 1)]


# ------------------------------------------------------ границы дозагрузки


def test_backfill_takes_only_between_horizon_and_fresh_window(tmp_path, conn):
    seed(
        conn,
        [
            (1, date(2026, 9, 24)),  # свежий — берёт ежедневный сбор
            (2, date(2026, 9, 10)),  # в зоне дозагрузки
            (3, date(2026, 7, 1)),  # старше горизонта — глубокая история
        ],
    )
    client = make_client(tmp_path, [EMPTY] * 3)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert sorted(calls(client)) == [("events", 2), ("lineups", 2), ("statistics", 2)]


def test_moving_horizon_adds_older_matches(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 10)), (2, date(2026, 7, 1))])
    first = make_client(tmp_path, [EMPTY] * 3)
    collect.run_backfill(first, conn, since=SINCE, today=TODAY)
    assert {fid for _, fid in calls(first)} == {1}

    second = make_client(tmp_path / "b", [EMPTY] * 3)
    collect.run_backfill(second, conn, since=date(2026, 6, 1), today=TODAY)

    assert {fid for _, fid in calls(second)} == {2}  # матч 1 уже собран


def test_horizon_after_fresh_boundary_gives_empty_queue(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 10))])
    client = make_client(tmp_path, [])

    ctx = collect.run_backfill(client, conn, since=date(2026, 12, 1), today=TODAY)

    assert ctx.items_processed == 0
    assert client.requests_used == 0


# ------------------------------------------------- деление остатка (ДЗ-4, ДЗ-5)


def test_backfill_splits_budget_equally_between_kinds(tmp_path, conn):
    seed(conn, [(i, date(2026, 9, 10)) for i in range(1, 7)])
    client = make_client(tmp_path, [EMPTY] * 9, max_requests=9)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY, chunk=3)

    by_kind = {k: sum(1 for kind, _ in calls(client) if kind == k) for k in collect.KINDS}
    assert by_kind == {"events": 3, "statistics": 3, "lineups": 3}


def test_backfill_share_of_empty_queue_goes_to_others(tmp_path, conn):
    """Статистики у источника нет — её доля достаётся событиям и составам."""
    seed(conn, [(i, date(2026, 9, 10)) for i in range(1, 7)], coverage=(True, False, True))
    # 8 запросов = два полных раунда по 2 матча на каждый из двух живых наборов.
    # Равенство точное лишь с точностью до одного раунда (chunk), поэтому бюджет
    # взят кратным раунду; при рабочих 50 матчах на набор разница незаметна.
    client = make_client(tmp_path, [EMPTY] * 8, max_requests=8)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY, chunk=2)

    by_kind = {k: sum(1 for kind, _ in calls(client) if kind == k) for k in collect.KINDS}
    assert by_kind == {"events": 4, "statistics": 0, "lineups": 4}


def test_backfill_goes_newest_first(tmp_path, conn):
    seed(conn, [(1, date(2026, 8, 1)), (2, date(2026, 9, 1)), (3, date(2026, 8, 15))])
    client = make_client(tmp_path, [EMPTY], max_requests=1)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert calls(client) == [("events", 2)]  # самый новый из трёх


# ------------------------------------------------- приоритет свежих (ДЗ-2, ДЗ-3)


def test_daily_spends_budget_on_fresh_before_backfill(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 24))] + [(i, date(2026, 9, 10)) for i in range(10, 16)])
    client = make_client(tmp_path, [EMPTY], max_requests=1)

    collect.run_daily(client, conn, today=TODAY)

    assert calls(client) == [("events", 1)]  # единственный запрос ушёл на свежий матч
    with conn.cursor() as cur:
        cur.execute("SELECT status, requests_used FROM collection_runs WHERE job_name = 'backfill'")
        assert cur.fetchone() == ("quota_exceeded", 0)  # штатно: остатка нет


def test_daily_backfill_uses_leftover_after_fresh(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 24))] + [(i, date(2026, 9, 10)) for i in range(10, 16)])
    # свежий матч: 3 запроса; остаток 4 уходит на дозагрузку
    client = make_client(tmp_path, [EMPTY] * 7, max_requests=7)

    collect.run_daily(client, conn, today=TODAY)

    fresh = [c for c in calls(client)[:3]]
    assert sorted(fresh) == [("events", 1), ("lineups", 1), ("statistics", 1)]
    assert all(fid >= 10 for _, fid in calls(client)[3:])  # дальше — только старые
    assert len(calls(client)) == 7


def test_daily_no_backfill_flag(tmp_path, conn):
    seed(conn, [(i, date(2026, 9, 10)) for i in range(10, 13)])
    client = make_client(tmp_path, [])

    results = collect.run_daily(client, conn, today=TODAY, backfill=False)

    assert client.requests_used == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM collection_runs WHERE job_name = 'backfill'")
        assert cur.fetchone()[0] == 0
    assert len(results) == 5


# --------------------------------------------- общее состояние и отбор (ДЗ-7)


def test_backfill_skips_already_collected_set(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 10))])
    with conn.cursor() as cur:
        cur.execute("INSERT INTO fixture_fetch_state (fixture_id, events_fetched_at) VALUES (1, now())")
    client = make_client(tmp_path, [EMPTY] * 2)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert sorted(calls(client)) == [("lineups", 1), ("statistics", 1)]


def test_backfill_skips_match_after_three_failures(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 10))])
    with conn.cursor() as cur:
        cur.execute("INSERT INTO fixture_fetch_state (fixture_id, events_attempts) VALUES (1, 3)")
    client = make_client(tmp_path, [EMPTY] * 2)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert "events" not in {k for k, _ in calls(client)}


def test_backfill_ignores_untracked_league(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 10))], tracked=False)
    client = make_client(tmp_path, [])

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert client.requests_used == 0


def test_backfill_ignores_kind_without_coverage(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 10))], coverage=(True, True, False))
    client = make_client(tmp_path, [EMPTY] * 2)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert "lineups" not in {k for k, _ in calls(client)}


def test_repeated_backfill_does_not_refetch(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 10))])
    collect.run_backfill(make_client(tmp_path, [EMPTY] * 3), conn, since=SINCE, today=TODAY)

    again = make_client(tmp_path / "b", [])
    collect.run_backfill(again, conn, since=SINCE, today=TODAY)

    assert again.requests_used == 0


# ------------------------------------------------- остановка и журнал (ДЗ-8, ДЗ-10)


def test_backfill_stops_gracefully_on_api_daily_limit(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 10)), (2, date(2026, 9, 11))])
    limit = {"errors": {"requests": "You have reached the request limit for the day"}}
    client = make_client(tmp_path, [limit, EMPTY])

    ctx = collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert client.quota_exhausted
    assert len(client.transport.calls) == 1
    assert ctx.items_processed == 0
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM collection_runs WHERE job_name = 'backfill'")
        assert cur.fetchone()[0] == "quota_exceeded"


def test_backfill_writes_one_journal_row_with_totals(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 10)), (2, date(2026, 9, 11))])
    client = make_client(tmp_path, [EMPTY] * 6)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, requests_used, items_processed FROM collection_runs "
            "WHERE job_name = 'backfill'"
        )
        assert cur.fetchall() == [("success", 6, 6)]


def test_backfill_prints_breakdown_by_kind(tmp_path, conn, capsys):
    seed(conn, [(1, date(2026, 9, 10))])
    collect.run_backfill(make_client(tmp_path, [EMPTY] * 3), conn, since=SINCE, today=TODAY)

    assert "события 1, статистика 1, составы 1" in capsys.readouterr().out


# ---------------------------------------------------------------- отчёт (ДЗ-9)


def test_report_counts_groups_and_eta(conn):
    seed(
        conn,
        [
            (1, date(2026, 9, 24)),  # свежий
            (2, date(2026, 9, 10)),  # дозагрузка
            (3, date(2026, 9, 11)),  # дозагрузка
            (4, date(2026, 7, 1)),  # глубокая история
        ],
    )

    report = collect.backfill_report(conn, since=SINCE, today=TODAY, avg_leftover=4)

    assert report["fresh"] == {"events": 1, "statistics": 1, "lineups": 1}
    assert report["backfill"] == {"events": 2, "statistics": 2, "lineups": 2}
    assert report["deep"] == {"events": 1, "statistics": 1, "lineups": 1}
    assert report["backfill_requests"] == 6
    assert report["eta_days"] == 2  # 6 запросов при 4 в сутки — округление вверх


def test_report_excludes_collected_and_uncovered(conn):
    seed(conn, [(1, date(2026, 9, 10))], coverage=(True, False, True))
    with conn.cursor() as cur:
        cur.execute("INSERT INTO fixture_fetch_state (fixture_id, events_fetched_at) VALUES (1, now())")

    report = collect.backfill_report(conn, since=SINCE, today=TODAY)

    assert report["backfill"] == {"events": 0, "statistics": 0, "lineups": 1}


def test_report_cli_needs_no_api_key(conn, monkeypatch, capsys):
    seed(conn, [(1, date(2026, 9, 10))])
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.delenv("APIFOOTBALL_KEY", raising=False)

    code = collect.main(["--job", "backfill-report", "--backfill-since", "2026-07-08"])

    assert code == 0
    out = capsys.readouterr().out
    assert "дозагрузка" in out and "глубокая история" in out
