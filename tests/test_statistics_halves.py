"""Тесты статистики по таймам (specs/статистика-по-таймам.md).

Подменённый транспорт и временная база, без сети (НФТ-8).
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")

from src.jobs import collect
from tests.test_backfill import SINCE, TODAY, calls, seed
from tests.test_collect import conn, make_client  # noqa: F401  (conn — фикстура)

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)

EMPTY = {"response": []}
OLD = date(2020, 3, 1)  # старше горизонта дозагрузки: ежедневный сбор и дозагрузка событий его не берут


def block(possession, shots=None, xg=None):
    rows = [{"type": "Ball Possession", "value": possession}]
    if shots is not None:
        rows.append({"type": "Shots on Goal", "value": shots})
    if xg is not None:
        rows.append({"type": "expected_goals", "value": xg})
    return rows


def team_entry(team_id, total, first=None, second=None):
    entry = {"team": {"id": team_id, "name": f"T{team_id}"}, "statistics": total}
    if first is not None:
        entry["statistics_1h"] = first
    if second is not None:
        entry["statistics_2h"] = second
    return entry


def full_body():
    return {
        "response": [
            team_entry(36, block("71%", 6, "1.50"), block("81%", 3, "0.90"), block("58%", 3, "0.60")),
            team_entry(34, block("29%", 2, "0.40"), block("19%", 1), block("42%", 1)),
        ]
    }


def seed_total_stats(conn, fixture_id, team_id=36, shots=9, possession=50):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fixture_statistics (fixture_id, team_id, shots_on_goal, ball_possession) "
            "VALUES (%s, %s, %s, %s)",
            (fixture_id, team_id, shots, possession),
        )


def halves_rows(conn, fixture_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT team_id, half, ball_possession, shots_on_goal, expected_goals "
            "FROM fixture_statistics_halves WHERE fixture_id = %s ORDER BY team_id, half",
            (fixture_id,),
        )
        return cur.fetchall()


def fetch_state(conn, fixture_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT statistics_fetched_at IS NOT NULL, halves_fetched_at IS NOT NULL, halves_attempts "
            "FROM fixture_fetch_state WHERE fixture_id = %s",
            (fixture_id,),
        )
        return cur.fetchone()


# ------------------------------------------------------- миграция (АП-1)


def test_halves_table_enforces_half_values(conn):
    seed(conn, [(1, OLD)])
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO fixture_statistics_halves (fixture_id, team_id, half) VALUES (1, 36, 3)"
            )


# --------------------------------------- ежедневный и общий шаг статистики (ПТ-3)


def test_statistics_step_writes_total_and_halves_with_one_request(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 24))])
    client = make_client(tmp_path, [full_body()])

    collect.run_statistics(client, conn, today=TODAY)

    assert client.requests_used == 1
    assert "half=true" in client.transport.calls[0]
    assert halves_rows(conn, 1) == [
        (34, 1, 19, 1, None),
        (34, 2, 42, 1, None),
        (36, 1, 81, 3, Decimal("0.90")),
        (36, 2, 58, 3, Decimal("0.60")),
    ]
    with conn.cursor() as cur:
        cur.execute("SELECT ball_possession FROM fixture_statistics WHERE fixture_id=1 AND team_id=36")
        assert cur.fetchone()[0] == 71
    assert fetch_state(conn, 1) == (True, True, 0)


def test_missing_half_blocks_are_not_an_error(tmp_path, conn):
    """ПТ-6: лига не отдаёт таймы — итог пишется, строк таймов нет, попытки не растут."""
    seed(conn, [(1, date(2026, 9, 24))])
    body = {"response": [team_entry(36, block("60%", 4)), team_entry(34, block("40%", 1))]}
    client = make_client(tmp_path, [body])

    collect.run_statistics(client, conn, today=TODAY)

    assert halves_rows(conn, 1) == []
    assert fetch_state(conn, 1) == (True, True, 0)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM fixture_statistics WHERE fixture_id = 1")
        assert cur.fetchone()[0] == 2


def test_all_null_half_block_is_skipped_and_partial_block_keeps_values(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 24))])
    only_null = [{"type": "Ball Possession", "value": None}, {"type": "Shots on Goal", "value": None}]
    body = {"response": [team_entry(36, block("50%"), only_null, block("52%"))]}
    client = make_client(tmp_path, [body])

    collect.run_statistics(client, conn, today=TODAY)

    assert halves_rows(conn, 1) == [(36, 2, 52, None, None)]


def test_unknown_stat_type_in_half_block_is_ignored(tmp_path, conn):
    seed(conn, [(1, date(2026, 9, 24))])
    first = block("55%", 2) + [{"type": "Mystery Metric", "value": 99}]
    client = make_client(tmp_path, [{"response": [team_entry(36, block("50%"), first, None)]}])

    collect.run_statistics(client, conn, today=TODAY)

    assert halves_rows(conn, 1) == [(36, 1, 55, 2, None)]


def test_half_rows_are_idempotent_and_updated(tmp_path, conn):
    """ПТ-5: повторная запись не создаёт дублей и обновляет значение."""
    seed(conn, [(1, date(2026, 9, 24))])
    collect._process_statistics(make_client(tmp_path, [full_body()]), conn, 1)
    changed = full_body()
    changed["response"][0]["statistics_1h"] = block("77%", 5, "1.10")
    collect._process_statistics(make_client(tmp_path / "b", [changed]), conn, 1)

    rows = halves_rows(conn, 1)
    assert len(rows) == 4
    assert (36, 1, 77, 5, Decimal("1.10")) in rows


# ------------------------------------------------- добор для старых матчей (ПТ-4)


def test_halves_backfill_picks_matches_with_stats_and_keeps_totals(tmp_path, conn):
    seed(conn, [(1, OLD), (2, OLD)])
    seed_total_stats(conn, 1)  # у матча 2 итоговой статистики нет — он не кандидат
    client = make_client(tmp_path, [full_body()])

    collect.run_backfill(client, conn, since=SINCE, today=TODAY, chunk=5)

    assert calls(client) == [("statistics", 1)]
    assert "half=true" in client.transport.calls[0]
    assert len(halves_rows(conn, 1)) == 4
    with conn.cursor() as cur:
        cur.execute("SELECT shots_on_goal, ball_possession FROM fixture_statistics WHERE fixture_id=1")
        assert cur.fetchone() == (9, 50)  # итог из ответа (71%) не перезаписал прежний
    assert fetch_state(conn, 1)[1] is True


def test_halves_backfill_does_not_repeat_finished_matches(tmp_path, conn):
    seed(conn, [(1, OLD)])
    seed_total_stats(conn, 1)
    collect.run_backfill(make_client(tmp_path, [full_body()]), conn, since=SINCE, today=TODAY)

    second = make_client(tmp_path / "b", [])
    collect.run_backfill(second, conn, since=SINCE, today=TODAY)

    assert second.requests_used == 0


def test_halves_backfill_gives_up_after_max_attempts(tmp_path, conn):
    seed(conn, [(1, OLD)])
    seed_total_stats(conn, 1)
    failing = {"errors": {"server": "boom"}, "response": []}
    client = make_client(tmp_path, [failing] * 10, max_requests=10)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert [c for c in calls(client) if c == ("statistics", 1)] == [("statistics", 1)] * collect.MAX_ATTEMPTS
    assert fetch_state(conn, 1) == (False, False, collect.MAX_ATTEMPTS)


def test_halves_backfill_covers_all_years_newest_first(tmp_path, conn):
    """Таймы берутся без горизонта дозагрузки, от новых матчей к старым (вариант Б)."""
    seed(conn, [(1, date(2018, 8, 1)), (2, date(2024, 5, 1)), (3, date(2021, 5, 1))])
    for fixture_id in (1, 2, 3):
        seed_total_stats(conn, fixture_id)
    client = make_client(tmp_path, [full_body()] * 3)

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert calls(client) == [("statistics", 2), ("statistics", 3), ("statistics", 1)]


def test_halves_go_before_events_and_lineups(tmp_path, conn):
    """Пока очередь таймов не пуста, события и составы дозагрузки ждут."""
    seed(conn, [(1, OLD), (2, date(2026, 9, 10))])
    seed_total_stats(conn, 1)
    client = make_client(tmp_path, [full_body(), EMPTY, EMPTY, EMPTY])

    collect.run_backfill(client, conn, since=SINCE, today=TODAY, chunk=5)

    first = calls(client)[0]
    assert first == ("statistics", 1)
    assert set(calls(client)[1:]) == {("events", 2), ("statistics", 2), ("lineups", 2)}


def test_budget_limits_halves_phase_and_stops_cleanly(tmp_path, conn):
    seed(conn, [(i, date(2024, 1, i)) for i in range(1, 5)])
    for fixture_id in range(1, 5):
        seed_total_stats(conn, fixture_id)
    client = make_client(tmp_path, [full_body()] * 2, max_requests=2)

    ctx = collect.run_backfill(client, conn, since=SINCE, today=TODAY, chunk=4)

    assert client.requests_used == 2
    assert ctx.items_processed == 2
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM collection_runs WHERE run_id = %s", (ctx.run_id,))
        assert cur.fetchone()[0] == "quota_exceeded"


def test_league_without_statistics_coverage_is_not_queued_for_halves(tmp_path, conn):
    seed(conn, [(1, OLD)], coverage=(True, False, True))
    seed_total_stats(conn, 1)
    client = make_client(tmp_path, [])

    collect.run_backfill(client, conn, since=SINCE, today=TODAY)

    assert client.requests_used == 0


# --------------------------------------------------------------- отчёт (АП-7)


def test_backfill_report_counts_halves_queue(conn, capsys):
    seed(conn, [(1, OLD), (2, OLD), (3, date(2026, 9, 10))])
    seed_total_stats(conn, 1)
    seed_total_stats(conn, 2)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO fixture_fetch_state (fixture_id, halves_fetched_at) VALUES (2, now())")

    report = collect.backfill_report(conn, since=SINCE, today=TODAY)

    assert report["halves"] == 1
    assert report["backfill_requests"] == 3 + 1  # матч 3: события, статистика, составы; плюс таймы матча 1
    collect.print_backfill_report(conn, SINCE, 3)
    assert "таймам" in capsys.readouterr().out
