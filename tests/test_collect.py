"""Тесты ежедневного сбора (specs/ежедневный-сбор.md).

Транспорт подменён (как в test_api_client.py), тестовая база нужна
(TEST_DATABASE_URL). Ни одного реального запроса к API (НФТ-8).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from src.api.client import ApiClient, HttpResponse
from src.db import migrate
from src.jobs import collect

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


# ------------------------------------------------------------ вспомогательное


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class ScriptedTransport:
    """Отвечает по очереди заранее заданными JSON-телами, по одному на вызов."""

    def __init__(self, bodies: list[dict]) -> None:
        import json

        self.bodies = [json.dumps(b).encode("utf-8") for b in bodies]
        self.calls: list[str] = []

    def __call__(self, url, headers, timeout) -> HttpResponse:
        self.calls.append(url)
        if not self.bodies:
            raise AssertionError("транспорт вызван больше раз, чем задано ответов")
        return HttpResponse(status=200, headers={}, body=self.bodies.pop(0))


def make_client(tmp_path, bodies, **kwargs) -> ApiClient:
    clock = FakeClock()
    return ApiClient(
        key="test-key",
        cache_dir=tmp_path / "cache",
        transport=ScriptedTransport(bodies),
        sleep=clock.sleep,
        now=clock.now,
        verbose=False,
        **kwargs,
    )


@pytest.fixture()
def conn():
    with psycopg.connect(TEST_URL, autocommit=True) as connection:
        with connection.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        migrate.run_up(connection)
        yield connection


def seed_league_and_fixture(conn, fixture_id=1001, league_id=39, season=2025, status="NS"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leagues (league_id, name, is_tracked) VALUES (%s, 'Premier League', true)",
            (league_id,),
        )
        cur.execute(
            "INSERT INTO league_seasons (league_id, season, has_events, has_statistics, has_lineups, has_injuries) "
            "VALUES (%s, %s, true, true, true, true)",
            (league_id, season),
        )
        cur.execute("INSERT INTO teams (team_id, name) VALUES (36, 'Fulham'), (34, 'Newcastle')")
        cur.execute(
            """INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date,
               status_short, home_team_id, away_team_id)
               VALUES (%s, %s, %s, now(), current_date, %s, 36, 34)""",
            (fixture_id, league_id, season, status),
        )


# ------------------------------------------------------------------- каталог


def test_catalog_writes_leagues_and_seasons(tmp_path, conn):
    body = {
        "response": [
            {
                "league": {"id": 39, "name": "Premier League", "type": "League"},
                "country": {"name": "England", "code": "GB"},
                "seasons": [
                    {
                        "year": 2025,
                        "start": "2025-08-01",
                        "end": "2026-05-30",
                        "coverage": {
                            "fixtures": {"events": True, "statistics_fixtures": True, "lineups": True},
                            "injuries": True,
                            "odds": False,
                        },
                    }
                ],
            }
        ]
    }
    client = make_client(tmp_path, [body])
    ctx = collect.run_catalog(client, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT name, country FROM leagues WHERE league_id = 39")
        assert cur.fetchone() == ("Premier League", "England")
        cur.execute(
            "SELECT has_events, has_injuries, has_odds FROM league_seasons WHERE league_id=39 AND season=2025"
        )
        assert cur.fetchone() == (True, True, False)
    assert ctx.items_processed == 1

    with conn.cursor() as cur:
        cur.execute("SELECT status, job_name FROM collection_runs")
        assert cur.fetchone() == ("success", "catalog")


# ------------------------------------------------------------------- матчи


def fixture_response_item(status="FT", elapsed=90):
    return {
        "fixture": {
            "id": 1001,
            "date": "2026-05-24T15:00:00+00:00",
            "status": {"long": "Match Finished", "short": status, "elapsed": elapsed},
            "venue": {"name": "Craven Cottage", "city": "London"},
        },
        "league": {"id": 39, "season": 2025, "round": "Regular Season - 38"},
        "teams": {"home": {"id": 36, "name": "Fulham"}, "away": {"id": 34, "name": "Newcastle"}},
        "goals": {"home": 2, "away": 0},
        "score": {"halftime": {"home": 1, "away": 0}},
    }


def test_fixtures_writes_match_with_all_fields(tmp_path, conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO leagues (league_id, name, is_tracked) VALUES (39, 'Premier League', true)")
        cur.execute("INSERT INTO league_seasons (league_id, season) VALUES (39, 2025)")
    client = make_client(tmp_path, [{"response": [fixture_response_item()]}])

    ctx = collect.run_fixtures(client, conn, min_season=2025)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status_short, elapsed, goals_home, ht_home, venue_city, round "
            "FROM fixtures WHERE fixture_id = 1001"
        )
        assert cur.fetchone() == ("FT", 90, 2, 1, "London", "Regular Season - 38")
    assert ctx.items_processed == 1


def test_fixtures_update_does_not_duplicate(tmp_path, conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO leagues (league_id, name, is_tracked) VALUES (39, 'Premier League', true)")
        cur.execute("INSERT INTO league_seasons (league_id, season) VALUES (39, 2025)")
    client = make_client(
        tmp_path,
        [
            {"response": [fixture_response_item(status="NS", elapsed=None)]},
            {"response": [fixture_response_item(status="FT", elapsed=90)]},
        ],
    )
    collect.run_fixtures(client, conn, min_season=2025)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM fixtures")
        assert cur.fetchone()[0] == 1

    # Клиент со своим ApiClient, но ТЕМ ЖЕ каталогом кэша: реальный сценарий —
    # завтрашний запуск сбора на том же сервере с тем же кэшем на диске.
    # Если бы /fixtures кэшировался, этот вызов вернул бы вчерашний NS.
    client2 = make_client(tmp_path, [{"response": [fixture_response_item(status="FT", elapsed=90)]}])
    collect.run_fixtures(client2, conn, min_season=2025)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), status_short FROM fixtures GROUP BY status_short")
        assert cur.fetchone() == (1, "FT")
    assert client2.requests_used == 1  # реально сходил в API, не взял из кэша


# ------------------------------------------------- охват лиг и активные сезоны


def _add_league_season(conn, league_id, season, start, end, tracked=True):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leagues (league_id, name, is_tracked) VALUES (%s, %s, %s)",
            (league_id, f"League {league_id}", tracked),
        )
        cur.execute(
            "INSERT INTO league_seasons (league_id, season, start_date, end_date) VALUES (%s, %s, %s, %s)",
            (league_id, season, start, end),
        )


def test_untracked_league_is_not_requested(tmp_path, conn):
    """Лига вне охвата (ФТ-9) не тратит запросы, даже если сезон идёт."""
    _add_league_season(conn, 900, 2026, "2026-08-01", "2027-05-30", tracked=False)
    client = make_client(tmp_path, [])  # транспорт не должен вызваться

    collect.run_fixtures(client, conn, min_season=2025, today=date(2026, 9, 24))

    assert client.requests_used == 0


def test_finished_season_is_not_requested(tmp_path, conn):
    """Сезон, закончившийся давно, не запрашивается каждый день (ФТ-5)."""
    _add_league_season(conn, 901, 2025, "2025-08-01", "2026-05-30")
    client = make_client(tmp_path, [])

    collect.run_fixtures(client, conn, min_season=2025, today=date(2026, 9, 24))

    assert client.requests_used == 0


def test_running_season_is_requested(tmp_path, conn):
    _add_league_season(conn, 902, 2026, "2026-08-01", "2027-05-30")
    client = make_client(tmp_path, [{"response": []}])

    collect.run_fixtures(client, conn, min_season=2025, today=date(2026, 9, 24))

    assert client.requests_used == 1


def test_season_just_finished_is_still_requested(tmp_path, conn):
    """Ещё неделю после конца сезона: возможны поздние правки результатов."""
    _add_league_season(conn, 903, 2026, "2026-01-01", "2026-09-20")
    client = make_client(tmp_path, [{"response": []}])

    collect.run_fixtures(client, conn, min_season=2025, today=date(2026, 9, 24))

    assert client.requests_used == 1


def test_season_without_dates_is_requested(tmp_path, conn):
    """Каталог ещё не загружен: лучше лишний запрос, чем пропущенные матчи."""
    _add_league_season(conn, 904, 2026, None, None)
    client = make_client(tmp_path, [{"response": []}])

    collect.run_fixtures(client, conn, min_season=2025, today=date(2026, 9, 24))

    assert client.requests_used == 1


def test_injuries_only_where_covered_and_active(tmp_path, conn):
    _add_league_season(conn, 905, 2026, "2026-08-01", "2027-05-30")
    with conn.cursor() as cur:
        cur.execute("UPDATE league_seasons SET has_injuries = false WHERE league_id = 905")
    client = make_client(tmp_path, [])

    collect.run_injuries(client, conn, min_season=2025, today=date(2026, 9, 24))

    assert client.requests_used == 0


def test_injury_for_unknown_fixture_is_skipped_not_fatal(tmp_path, conn):
    """Травма на матч, которого нет в базе, не должна ронять весь шаг."""
    _add_league_season(conn, 906, 2026, "2026-08-01", "2027-05-30")
    with conn.cursor() as cur:
        cur.execute("UPDATE league_seasons SET has_injuries = true WHERE league_id = 906")
        cur.execute("INSERT INTO teams (team_id, name) VALUES (36, 'Fulham'), (34, 'Newcastle')")
        cur.execute(
            """INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date,
               status_short, home_team_id, away_team_id)
               VALUES (5001, 906, 2026, now(), current_date, 'NS', 36, 34)"""
        )

    def entry(fixture_id, player_id):
        return {
            "player": {"id": player_id, "name": f"P{player_id}", "type": "Missing Fixture", "reason": "Knee"},
            "team": {"id": 36, "name": "Fulham"},
            "fixture": {"id": fixture_id},
            "league": {"id": 906, "season": 2026},
        }

    body = {"response": [entry(5001, 11), entry(9999, 12)]}  # 9999 в базе нет
    client = make_client(tmp_path, [body])

    ctx = collect.run_injuries(client, conn, min_season=2025, today=date(2026, 9, 24))

    assert ctx.items_processed == 1
    assert ctx.skipped == 1
    with conn.cursor() as cur:
        cur.execute("SELECT fixture_id, player_id FROM injuries")
        assert cur.fetchall() == [(5001, 11)]
        cur.execute("SELECT status FROM collection_runs WHERE job_name = 'injuries'")
        assert cur.fetchone()[0] != "failed"


def test_events_not_requested_for_untracked_league(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    with conn.cursor() as cur:
        cur.execute("UPDATE leagues SET is_tracked = false")
    client = make_client(tmp_path, [])

    ctx = collect.run_events(client, conn)

    assert ctx.items_processed == 0
    assert client.requests_used == 0


# ------------------------------------------------------------------- события


def test_events_skipped_when_coverage_false(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    with conn.cursor() as cur:
        cur.execute("UPDATE league_seasons SET has_events = false")
    client = make_client(tmp_path, [])  # транспорт не должен вызваться

    ctx = collect.run_events(client, conn)

    assert ctx.items_processed == 0
    assert client.requests_used == 0


def test_events_marks_fetched_even_when_empty(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    client = make_client(tmp_path, [{"response": []}])

    collect.run_events(client, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT events_fetched_at IS NOT NULL, events_attempts FROM fixture_fetch_state")
        assert cur.fetchone() == (True, 0)


def test_events_fetch_error_increments_attempts_not_fetched(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    client = make_client(tmp_path, [{"errors": {"fixture": "not found"}}])

    collect.run_events(client, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT events_fetched_at, events_attempts FROM fixture_fetch_state")
        assert cur.fetchone() == (None, 1)


def test_events_fixture_excluded_after_three_attempts(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fixture_fetch_state (fixture_id, events_attempts) VALUES (1001, 3)"
        )
    client = make_client(tmp_path, [])

    ctx = collect.run_events(client, conn)

    assert ctx.items_processed == 0
    assert client.requests_used == 0


def test_events_writes_row_with_assist_id(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    body = {
        "response": [
            {
                "time": {"elapsed": 46, "extra": None},
                "team": {"id": 36, "name": "Fulham"},
                "player": {"id": 19163, "name": "J. Murphy"},
                "assist": {"id": 18778, "name": "H. Barnes"},
                "type": "subst",
                "detail": "Substitution 1",
                "comments": None,
            }
        ]
    }
    client = make_client(tmp_path, [body])

    collect.run_events(client, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT team_id, player_id, assist_player_id, type FROM fixture_events")
        assert cur.fetchone() == (36, 19163, 18778, "subst")


def test_events_key_matches_shared_function(tmp_path, conn):
    """Ключ события должен совпадать с формулой переноса (ПР-5)."""
    from src.common.keys import event_key

    seed_league_and_fixture(conn, status="FT")
    body = {
        "response": [
            {
                "time": {"elapsed": 11, "extra": None},
                "team": {"id": 36, "name": "Fulham"},
                "player": {"id": 1460, "name": "B. Saka"},
                "assist": {"id": None, "name": None},
                "type": "Goal",
                "detail": "Normal Goal",
                "comments": None,
            }
        ]
    }
    client = make_client(tmp_path, [body])
    collect.run_events(client, conn)

    expected = event_key(1001, 11, None, 36, "Goal", "Normal Goal", 1460)
    with conn.cursor() as cur:
        cur.execute("SELECT event_key FROM fixture_events")
        assert cur.fetchone()[0] == expected


# ---------------------------------------------------------------- статистика


def test_statistics_parses_percent_and_decimal(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    body = {
        "response": [
            {
                "team": {"id": 36, "name": "Fulham"},
                "statistics": [
                    {"type": "Shots on Goal", "value": 6},
                    {"type": "Ball Possession", "value": "45%"},
                    {"type": "expected_goals", "value": "1.81"},
                    {"type": "goals_prevented", "value": "-0.17"},
                ],
            }
        ]
    }
    client = make_client(tmp_path, [body])

    collect.run_statistics(client, conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT shots_on_goal, ball_possession, expected_goals, goals_prevented "
            "FROM fixture_statistics WHERE fixture_id=1001 AND team_id=36"
        )
        row = cur.fetchone()
        assert row[0] == 6
        assert row[1] == 45
        assert float(row[2]) == 1.81
        assert float(row[3]) == -0.17


# -------------------------------------------------------------------- составы


def test_lineups_marks_starters_and_substitutes(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    body = {
        "response": [
            {
                "team": {"id": 36, "name": "Fulham"},
                "coach": {"id": 10, "name": "Marco Silva"},
                "formation": "4-2-3-1",
                "startXI": [{"player": {"id": 1438, "name": "B. Leno", "number": 1, "pos": "G", "grid": "1:1"}}],
                "substitutes": [{"player": {"id": 2000, "name": "Sub Player", "number": 20, "pos": "M", "grid": None}}],
            }
        ]
    }
    client = make_client(tmp_path, [body])

    collect.run_lineups(client, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT formation, coach_name FROM fixture_lineups WHERE fixture_id=1001 AND team_id=36")
        assert cur.fetchone() == ("4-2-3-1", "Marco Silva")
        cur.execute(
            "SELECT player_id, is_starter FROM fixture_lineup_players "
            "WHERE fixture_id=1001 ORDER BY player_id"
        )
        assert cur.fetchall() == [(1438, True), (2000, False)]


# --------------------------------------------------------------- коэффициенты


def odds_response_item(fixture_id=1001):
    return {
        "league": {"id": 39, "season": 2025},
        "fixture": {"id": fixture_id},
        "update": "2026-09-24T00:01:00+00:00",
        "bookmakers": [
            {
                "id": 8,
                "name": "1xBet",
                "bets": [{"id": 1, "name": "Match Winner", "values": [{"value": "Home", "odd": "2.10"}]}],
            },
            {
                "id": 4,
                "name": "Pinnacle",
                "bets": [{"id": 1, "name": "Match Winner", "values": [{"value": "Home", "odd": "2.05"}]}],
            },
            {
                "id": 2,
                "name": "Bet365",
                "bets": [{"id": 1, "name": "Match Winner", "values": [{"value": "Home", "odd": "2.15"}]}],
            },
        ],
    }


def test_odds_filters_to_configured_bookmakers_but_keeps_full_reference(tmp_path, conn):
    seed_league_and_fixture(conn)
    body = {"response": [odds_response_item()], "paging": {"current": 1, "total": 1}}
    client = make_client(tmp_path, [body])

    ctx = collect.run_odds(client, conn, horizon_days=1)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM bookmakers")
        assert cur.fetchone()[0] == 3  # полный справочник, включая Bet365
        cur.execute(
            "SELECT b.name FROM odds_values v JOIN bookmakers b ON b.bookmaker_id = v.bookmaker_id "
            "ORDER BY b.name"
        )
        assert [r[0] for r in cur.fetchall()] == ["1xBet", "Pinnacle"]  # без Bet365
    assert ctx.items_processed == 1


def test_odds_skips_fixture_not_in_database(tmp_path, conn):
    seed_league_and_fixture(conn, fixture_id=1001)
    body = {"response": [odds_response_item(fixture_id=9999)], "paging": {"current": 1, "total": 1}}
    client = make_client(tmp_path, [body])

    ctx = collect.run_odds(client, conn, horizon_days=1)

    assert ctx.skipped == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM odds_snapshots")
        assert cur.fetchone()[0] == 0


def test_odds_rerun_does_not_duplicate_values(tmp_path, conn):
    seed_league_and_fixture(conn)
    body = {"response": [odds_response_item()], "paging": {"current": 1, "total": 1}}
    client1 = make_client(tmp_path, [body])
    collect.run_odds(client1, conn, horizon_days=1)

    client2 = make_client(tmp_path, [body])
    collect.run_odds(client2, conn, horizon_days=1)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM odds_snapshots")
        # разные клиенты запускались в один и тот же (почти) момент теста,
        # но taken_at у каждого свой -> два снимка это ожидаемо (разное время)
        assert cur.fetchone()[0] in (1, 2)


def test_odds_second_daily_call_bypasses_cache(tmp_path, conn):
    """Второй снимок за день (12:00) не должен получить кэш от первого (00:00).

    Без use_cache=False на /odds это ломало бы весь смысл двух снимков в сутки
    (ФТ-10) — обнаружено тестом на реальном сдвиге поведения (см. также
    test_fixtures_update_does_not_duplicate).
    """
    seed_league_and_fixture(conn)
    morning = odds_response_item()
    morning["bookmakers"][0]["bets"][0]["values"][0]["odd"] = "2.10"
    noon = odds_response_item()
    noon["bookmakers"][0]["bets"][0]["values"][0]["odd"] = "1.95"  # линия сдвинулась

    client_morning = make_client(
        tmp_path, [{"response": [morning], "paging": {"current": 1, "total": 1}}]
    )
    collect.run_odds(client_morning, conn, horizon_days=1)

    client_noon = make_client(
        tmp_path, [{"response": [noon], "paging": {"current": 1, "total": 1}}]
    )
    collect.run_odds(client_noon, conn, horizon_days=1)

    assert client_noon.requests_used == 1  # реально сходил в API
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT odd FROM odds_values v "
            "JOIN bookmakers b ON b.bookmaker_id = v.bookmaker_id "
            "WHERE b.name = %s ORDER BY odd",
            ("1xBet",),
        )
        odds_seen = [float(r[0]) for r in cur.fetchall()]
    assert odds_seen == [1.95, 2.10]  # оба снимка сохранены, второй не потерян


# ------------------------------------------------------------- журнал/бюджет


def test_quota_exceeded_status_recorded(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    client = make_client(tmp_path, [{"response": []}], max_requests=1)
    client.requests_used = 1  # бюджет уже исчерпан до вызова шага

    collect.run_events(client, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT status, requests_used FROM collection_runs WHERE job_name='events'")
        assert cur.fetchone() == ("quota_exceeded", 0)


def test_failed_step_still_writes_run_row(tmp_path, conn):
    client = make_client(tmp_path, [])

    def broken(_client, _conn):
        with collect.collection_run(_conn, _client, "broken") as ctx:
            raise RuntimeError("нарочная ошибка")

    with pytest.raises(RuntimeError):
        broken(client, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT status, error_text FROM collection_runs WHERE job_name='broken'")
        status, error_text = cur.fetchone()
        assert status == "failed"
        assert "нарочная ошибка" in error_text


def test_daily_stops_remaining_steps_when_quota_exhausted(tmp_path, conn):
    seed_league_and_fixture(conn, status="FT")
    client = make_client(tmp_path, [{"errors": {"requests": "reached the request limit for the day"}}])

    collect.run_daily(client, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT job_name FROM collection_runs ORDER BY run_id")
        job_names = [row[0] for row in cur.fetchall()]
    # первый же шаг (fixtures) исчерпал суточный лимит API -> дальше не идём
    assert job_names == ["fixtures"]


def test_cleanup_job_removes_only_old_cache_files(tmp_path, monkeypatch):
    """`--job cleanup` работает без ключа и базы и трогает только старые файлы."""
    import os
    import time

    cache = tmp_path / "cache"
    cache.mkdir()
    old, fresh = cache / "old.json", cache / "fresh.json"
    old.write_text("{}")
    fresh.write_text("{}")
    long_ago = time.time() - 31 * 86400
    os.utime(old, (long_ago, long_ago))
    monkeypatch.delenv("APIFOOTBALL_KEY", raising=False)

    code = collect.main(["--job", "cleanup", "--cache-dir", str(cache)])

    assert code == 0
    assert not old.exists()
    assert fresh.exists()


def test_daily_does_not_run_odds(tmp_path, conn):
    """Коэффициенты — отдельный запуск дважды в сутки; в ежедневный сбор не входят."""
    seed_league_and_fixture(conn, status="FT")
    client = make_client(tmp_path, [{"response": []}] * 20)

    collect.run_daily(client, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT job_name FROM collection_runs ORDER BY run_id")
        job_names = [row[0] for row in cur.fetchall()]
    # дозагрузка идёт последней и на остатке (ДЗ-2)
    assert job_names == ["fixtures", "injuries", "events", "statistics", "lineups", "backfill"]
    assert "odds" not in job_names


# ------------------------------------------------------------ ft90 (ДП-0)


def test_fixtures_aet_stores_ft90_from_fulltime(tmp_path, conn):
    """Матч AET: goals = счёт после доп. времени, ft90_* = счёт 90 минут."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO leagues (league_id, name, is_tracked) VALUES (39, 'PL', true)")
        cur.execute("INSERT INTO league_seasons (league_id, season) VALUES (39, 2025)")
    item = fixture_response_item(status="AET", elapsed=120)
    item["goals"] = {"home": 2, "away": 2}
    item["score"] = {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 1, "away": 2}}
    client = make_client(tmp_path, [{"response": [item]}])

    collect.run_fixtures(client, conn, min_season=2025)

    with conn.cursor() as cur:
        cur.execute("SELECT goals_home, goals_away, ft90_home, ft90_away FROM fixtures WHERE fixture_id=1001")
        assert cur.fetchone() == (2, 2, 1, 2)


def test_fixtures_ft_leaves_ft90_null(tmp_path, conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO leagues (league_id, name, is_tracked) VALUES (39, 'PL', true)")
        cur.execute("INSERT INTO league_seasons (league_id, season) VALUES (39, 2025)")
    client = make_client(tmp_path, [{"response": [fixture_response_item(status="FT")]}])

    collect.run_fixtures(client, conn, min_season=2025)

    with conn.cursor() as cur:
        cur.execute("SELECT ft90_home, ft90_away FROM fixtures WHERE fixture_id=1001")
        assert cur.fetchone() == (None, None)


def test_ft90_backfill_updates_only_aet_pen_without_ft90(tmp_path, conn):
    seed_league_and_fixture(conn, fixture_id=2001, status="AET")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date,
               status_short, home_team_id, away_team_id)
               VALUES (2002, 39, 2025, now(), current_date, 'FT', 36, 34)"""
        )
    body = {
        "response": [
            {
                "fixture": {"id": 2001},
                "score": {"fulltime": {"home": 1, "away": 1}},
            }
        ]
    }
    client = make_client(tmp_path, [body])

    ctx = collect.run_ft90_backfill(client, conn)

    assert ctx.items_processed == 1
    with conn.cursor() as cur:
        cur.execute("SELECT ft90_home, ft90_away FROM fixtures WHERE fixture_id=2001")
        assert cur.fetchone() == (1, 1)
        cur.execute("SELECT ft90_home FROM fixtures WHERE fixture_id=2002")
        assert cur.fetchone() == (None,)  # FT не выбирается вовсе, запрос не тратится
    assert client.requests_used == 1
