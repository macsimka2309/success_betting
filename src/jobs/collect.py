"""Ежедневный сбор: семь шагов из docs/02-архитектура.md.

Спецификация: specs/ежедневный-сбор.md

Стадия 1 (скачивание в кэш) делает ApiClient. Этот модуль — стадия 2
(ADR-2): разбор ответов из кэша/API в строки таблиц и запись с защитой
от дублей. Каждый шаг фиксирует себя в collection_runs (ФТ-6) и уважает
общий бюджет запросов клиента (ФТ-5).

Использование:
    python3 -m src.jobs.collect --job catalog
    python3 -m src.jobs.collect --job daily   # матчи, травмы, события, статистика, составы
    python3 -m src.jobs.collect --job odds [--odds-horizon-days N]
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterator

import psycopg

from src.api.client import ApiClient
from src.common.convert import to_decimal, to_int, to_percent, to_text, to_utc
from src.common.db import write_batch, write_row
from src.common.keys import event_key
from src.db.connection import connect, database_url

# Букмекеры, для которых сохраняются котировки (ФТ-10). Настройка, не
# константа кода в строгом смысле: список читается отсюда, но меняется
# правкой файла — при расширении список кэш на диске уже есть за 30 дней
# (ADR-2), повторный разбор не требует новых запросов.
DEFAULT_BOOKMAKERS = {"1xBet", "Pinnacle"}

# Сезон считается активным за 30 дней до старта и ещё 7 дней после конца:
# расписание и результаты завершённого сезона больше не меняются, и запрашивать
# их каждый день — трата суточного лимита впустую (ФТ-5).
ACTIVE_BEFORE_DAYS = 30
ACTIVE_AFTER_DAYS = 7

FINISHED_STATUSES = ("FT", "AET", "PEN")
MAX_ATTEMPTS = 3

# Свежие матчи — последние FRESH_DAYS дней: их события, статистику и составы
# ежедневный сбор берёт с приоритетом. Всё старше — дозагрузка на остатке
# лимита (specs/дозагрузка-пропущенного.md, ДЗ-1, ДЗ-3).
FRESH_DAYS = 3
# Горизонт дозагрузки: день, с которого прервался сбор (docs/00).
DEFAULT_BACKFILL_SINCE = date(2026, 7, 8)
# Дозагрузка идёт раундами: по столько матчей на каждый набор за раунд (ДЗ-4).
BACKFILL_CHUNK = 50

# API -> наши колонки статистики (проверено на живом ответе 24.09.2026).
STAT_TYPE_MAP = {
    "Shots on Goal": "shots_on_goal",
    "Shots off Goal": "shots_off_goal",
    "Total Shots": "total_shots",
    "Blocked Shots": "blocked_shots",
    "Shots insidebox": "shots_insidebox",
    "Shots outsidebox": "shots_outsidebox",
    "Fouls": "fouls",
    "Corner Kicks": "corner_kicks",
    "Offsides": "offsides",
    "Yellow Cards": "yellow_cards",
    "Red Cards": "red_cards",
    "Goalkeeper Saves": "goalkeeper_saves",
    "Total passes": "total_passes",
    "Passes accurate": "passes_accurate",
}
STAT_PERCENT_TYPES = {"Ball Possession": "ball_possession", "Passes %": "passes_pct"}
STAT_DECIMAL_TYPES = {"expected_goals": "expected_goals", "goals_prevented": "goals_prevented"}


class Stop(Exception):
    """Бюджет клиента исчерпан — шаг прекращает перебор дальнейших элементов."""


# ------------------------------------------------------------- журнал запусков


@dataclass
class StepContext:
    run_id: int
    items_processed: int = 0
    skipped: int = 0


@contextmanager
def collection_run(conn: psycopg.Connection, client: ApiClient, job_name: str) -> Iterator[StepContext]:
    """Открывает и закрывает строку collection_runs вокруг шага (ФТ-6, ЕС-1)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_runs (job_name, status) VALUES (%s, 'running') RETURNING run_id",
            (job_name,),
        )
        run_id = cur.fetchone()[0]
    requests_before = client.requests_used
    ctx = StepContext(run_id=run_id)
    try:
        yield ctx
    except Exception as exc:
        _finish_run(conn, run_id, "failed", client.requests_used - requests_before, ctx, str(exc)[:2000])
        raise
    else:
        budget_hit = client.quota_exhausted or client.requests_used >= client.max_requests
        status = "quota_exceeded" if budget_hit else "success"
        _finish_run(conn, run_id, status, client.requests_used - requests_before, ctx)


def _finish_run(conn, run_id: int, status: str, requests_used: int, ctx: StepContext, error_text: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE collection_runs SET finished_at = now(), status = %s,
               requests_used = %s, items_processed = %s, error_text = %s
               WHERE run_id = %s""",
            (status, requests_used, ctx.items_processed, error_text, run_id),
        )


# ------------------------------------------------------------ fixture_fetch_state


_ATTEMPT_COLUMN = {
    "events": "events_attempts",
    "statistics": "statistics_attempts",
    "lineups": "lineups_attempts",
}
_FETCHED_COLUMN = {
    "events": "events_fetched_at",
    "statistics": "statistics_fetched_at",
    "lineups": "lineups_fetched_at",
}


def mark_attempt(conn, fixture_id: int, field_name: str) -> None:
    column = _ATTEMPT_COLUMN[field_name]
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO fixture_fetch_state (fixture_id, {column}) VALUES (%s, 1)
                ON CONFLICT (fixture_id) DO UPDATE
                SET {column} = fixture_fetch_state.{column} + 1""",
            (fixture_id,),
        )


def mark_fetched(conn, fixture_id: int, field_name: str) -> None:
    column = _FETCHED_COLUMN[field_name]
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO fixture_fetch_state (fixture_id, {column}) VALUES (%s, now())
                ON CONFLICT (fixture_id) DO UPDATE SET {column} = now()""",
            (fixture_id,),
        )


def _pending_fixtures(
    conn,
    field_name: str,
    coverage_column: str,
    limit: int,
    min_date: date | None = None,
    max_date: date | None = None,
) -> list[int]:
    """Матчи, где данных ещё нет, а источник их отдаёт (ЕС-4, ЕС-5).

    `min_date` (включительно) и `max_date` (не включая) ограничивают дату матча:
    так ежедневный сбор берёт свежие, а дозагрузка — более старые.
    """
    fetched_column = _FETCHED_COLUMN[field_name]
    attempt_column = _ATTEMPT_COLUMN[field_name]
    params: list = [list(FINISHED_STATUSES), MAX_ATTEMPTS]
    date_filter = ""
    if min_date is not None:
        date_filter += " AND f.match_date >= %s"
        params.append(min_date)
    if max_date is not None:
        date_filter += " AND f.match_date < %s"
        params.append(max_date)
    params.append(limit)
    sql = f"""
        SELECT f.fixture_id
        FROM fixtures f
        JOIN leagues l ON l.league_id = f.league_id AND l.is_tracked
        LEFT JOIN league_seasons ls ON ls.league_id = f.league_id AND ls.season = f.season
        LEFT JOIN fixture_fetch_state s ON s.fixture_id = f.fixture_id
        WHERE f.status_short = ANY(%s)
          AND COALESCE(ls.{coverage_column}, true)
          AND (s.fixture_id IS NULL OR (
                s.{fetched_column} IS NULL AND COALESCE(s.{attempt_column}, 0) < %s
          )){date_filter}
        ORDER BY f.match_date DESC, f.fixture_id
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [row[0] for row in cur.fetchall()]


def _fetch_items(client: ApiClient, endpoint: str, fixture_id: int) -> list | None:
    """Возвращает список из `response`, None при неудаче, бросает Stop при бюджете."""
    body = client.get(endpoint, {"fixture": fixture_id})
    if body is None:
        if client.quota_exhausted or client.requests_used >= client.max_requests:
            raise Stop()
        return None
    if body.get("errors"):
        return None
    return body.get("response") or []


def _upsert_team(conn, team_id: int | None, name: str | None) -> None:
    if team_id is None:
        return
    write_row(conn, "teams", ["team_id", "name"], (team_id, name or f"Команда {team_id}"), "team_id")


def _upsert_player(conn, player_id: int | None, name: str | None) -> None:
    if player_id is None:
        return
    write_row(conn, "players", ["player_id", "name"], (player_id, name or f"Игрок {player_id}"), "player_id")


# --------------------------------------------------------------- шаг 1: каталог


def run_catalog(client: ApiClient, conn) -> StepContext:
    """Каталог лиг и покрытие по сезонам (ФТ-9, раз в неделю)."""
    with collection_run(conn, client, "catalog") as ctx:
        # Кэш не используется: цель еженедельного запуска — увидеть изменения
        # в каталоге, а не получить тот же ответ, что неделю назад (ADR-2
        # касается неизменных фактов о сыгранных матчах, не этого).
        items = client.paged("/leagues", {}, use_cache=False)
        for item in items:
            league = item.get("league") or {}
            country = item.get("country") or {}
            league_id = to_int(league.get("id"))
            if league_id is None:
                ctx.skipped += 1
                continue
            write_row(
                conn,
                "leagues",
                ["league_id", "name", "type", "country", "country_code"],
                (
                    league_id,
                    to_text(league.get("name")) or f"Лига {league_id}",
                    to_text(league.get("type")),
                    to_text(country.get("name")),
                    to_text(country.get("code")),
                ),
                "league_id",
                update_columns=["name", "type", "country", "country_code"],
            )
            for season_item in item.get("seasons") or []:
                season = to_int(season_item.get("year"))
                if season is None:
                    continue
                coverage = season_item.get("coverage") or {}
                fixtures_cov = coverage.get("fixtures") or {}
                write_row(
                    conn,
                    "league_seasons",
                    [
                        "league_id",
                        "season",
                        "start_date",
                        "end_date",
                        "has_events",
                        "has_statistics",
                        "has_lineups",
                        "has_injuries",
                        "has_odds",
                    ],
                    (
                        league_id,
                        season,
                        to_text(season_item.get("start")),
                        to_text(season_item.get("end")),
                        fixtures_cov.get("events"),
                        fixtures_cov.get("statistics_fixtures"),
                        fixtures_cov.get("lineups"),
                        coverage.get("injuries"),
                        coverage.get("odds"),
                    ),
                    "league_id, season",
                )
            ctx.items_processed += 1
    return ctx


# ------------------------------------------------------------- шаг 2: матчи


def _target_league_seasons(
    conn, min_season: int, require_column: str | None = None, today: date | None = None
) -> list[tuple[int, int]]:
    """Пары лига-сезон для запроса: только отслеживаемые лиги и активные сезоны.

    Сезон без дат (каталог ещё не загружен) считается активным: безопаснее
    лишний запрос, чем пропущенные матчи. `require_column` добавляет условие
    покрытия, например has_injuries.
    """
    today = today or datetime.now(timezone.utc).date()
    coverage = f"AND COALESCE(ls.{require_column}, true)" if require_column else ""
    sql = f"""
        SELECT ls.league_id, ls.season
        FROM league_seasons ls
        JOIN leagues l ON l.league_id = ls.league_id
        WHERE l.is_tracked
          AND ls.season >= %s
          AND (ls.start_date IS NULL OR ls.end_date IS NULL
               OR (ls.start_date - %s::int <= %s::date AND %s::date <= ls.end_date + %s::int))
          {coverage}
        ORDER BY ls.league_id, ls.season
    """
    with conn.cursor() as cur:
        cur.execute(sql, (min_season, ACTIVE_BEFORE_DAYS, today, today, ACTIVE_AFTER_DAYS))
        return [(row[0], row[1]) for row in cur.fetchall()]


FIXTURE_COLUMNS = (
    "fixture_id", "league_id", "season", "kickoff_at", "match_date",
    "round", "status_short", "status_long", "elapsed",
    "home_team_id", "away_team_id", "goals_home", "goals_away",
    "ht_home", "ht_away", "venue_name", "venue_city",
)


def _parse_fixture_item(item: dict, requested_league: int, requested_season: int) -> dict | None:
    """Разбирает один элемент ответа /fixtures в словарь по имени колонки.

    Словарь, а не позиционный кортеж: при матче с fixture_events/fixture_events
    путаница индексов давала молчаливый сдвиг колонок (найдено тестами) —
    имена читаются сами за себя и такую ошибку ловит любой ключ с опечаткой.
    """
    fixture = item.get("fixture") or {}
    league = item.get("league") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}
    score = item.get("score") or {}
    status = fixture.get("status") or {}
    venue = fixture.get("venue") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    halftime = score.get("halftime") or {}

    fixture_id = to_int(fixture.get("id"))
    kickoff = to_utc(fixture.get("date"))
    home_id = to_int(home.get("id"))
    away_id = to_int(away.get("id"))
    if fixture_id is None or kickoff is None or home_id is None or away_id is None:
        return None

    return {
        "fixture_id": fixture_id,
        "league_id": to_int(league.get("id")) or requested_league,
        "season": to_int(league.get("season")) or requested_season,
        "kickoff_at": kickoff,
        "match_date": kickoff.date(),
        "round": to_text(league.get("round")),
        "status_short": to_text(status.get("short")),
        "status_long": to_text(status.get("long")),
        "elapsed": to_int(status.get("elapsed")),
        "home_team_id": home_id,
        "away_team_id": away_id,
        "goals_home": to_int(goals.get("home")),
        "goals_away": to_int(goals.get("away")),
        "ht_home": to_int(halftime.get("home")),
        "ht_away": to_int(halftime.get("away")),
        "venue_name": to_text(venue.get("name")),
        "venue_city": to_text(venue.get("city")),
        "home_team_name": to_text(home.get("name")),
        "away_team_name": to_text(away.get("name")),
    }


def run_fixtures(
    client: ApiClient, conn, min_season: int | None = None, today: date | None = None
) -> StepContext:
    """Матчи по отслеживаемым лигам в активных сезонах (ФТ-2, ФТ-5, ФТ-9)."""
    if min_season is None:
        min_season = datetime.now(timezone.utc).year - 1
    with collection_run(conn, client, "fixtures") as ctx:
        for league_id, season in _target_league_seasons(conn, min_season, today=today):
            if client.quota_exhausted or client.requests_used >= client.max_requests:
                break
            # Кэш не используется: статус и счёт матча меняются день ото дня
            # при тех же параметрах запроса (лига+сезон) — закэшированный ответ
            # никогда не показал бы обновлённый результат (ФТ-3).
            body = client.get(
                "/fixtures", {"league": league_id, "season": season}, use_cache=False
            )
            if body is None or body.get("errors"):
                continue
            for item in body.get("response") or []:
                parsed = _parse_fixture_item(item, league_id, season)
                if parsed is None:
                    ctx.skipped += 1
                    continue
                _upsert_team(conn, parsed["home_team_id"], parsed["home_team_name"])
                _upsert_team(conn, parsed["away_team_id"], parsed["away_team_name"])
                write_row(
                    conn,
                    "fixtures",
                    list(FIXTURE_COLUMNS),
                    tuple(parsed[c] for c in FIXTURE_COLUMNS),
                    "fixture_id",
                )
                ctx.items_processed += 1
    return ctx


# ------------------------------------------------------------- шаг 3: травмы


def _existing_fixture_ids(conn, ids: set[int]) -> set[int]:
    if not ids:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT fixture_id FROM fixtures WHERE fixture_id = ANY(%s)", (list(ids),))
        return {row[0] for row in cur.fetchall()}


def run_injuries(
    client: ApiClient, conn, min_season: int | None = None, today: date | None = None
) -> StepContext:
    """Травмы: отслеживаемые лиги, активные сезоны, покрытие has_injuries (ФТ-5)."""
    if min_season is None:
        min_season = datetime.now(timezone.utc).year - 1
    pairs = _target_league_seasons(conn, min_season, require_column="has_injuries", today=today)

    with collection_run(conn, client, "injuries") as ctx:
        for league_id, season in pairs:
            if client.quota_exhausted or client.requests_used >= client.max_requests:
                break
            # Кэш не используется по той же причине, что у матчей: список
            # травм на сезон меняется день ото дня.
            body = client.get(
                "/injuries", {"league": league_id, "season": season}, use_cache=False
            )
            if body is None or body.get("errors"):
                continue
            items = body.get("response") or []
            # Травма может сослаться на матч, которого ещё нет в базе (например,
            # бюджет шага «матчи» кончился раньше): внешний ключ уронил бы весь
            # шаг. Такие строки пропускаются и подхватятся на следующий день.
            known = _existing_fixture_ids(
                conn, {to_int((i.get("fixture") or {}).get("id")) for i in items} - {None}
            )
            for item in items:
                fixture_id = to_int((item.get("fixture") or {}).get("id"))
                player = item.get("player") or {}
                team = item.get("team") or {}
                league_item = item.get("league") or {}
                player_id = to_int(player.get("id"))
                team_id = to_int(team.get("id"))
                if fixture_id is None or player_id is None or team_id is None:
                    ctx.skipped += 1
                    continue
                if fixture_id not in known:
                    ctx.skipped += 1
                    continue
                _upsert_player(conn, player_id, player.get("name"))
                _upsert_team(conn, team_id, team.get("name"))
                write_row(
                    conn,
                    "injuries",
                    ["fixture_id", "player_id", "team_id", "league_id", "season", "type", "reason"],
                    (
                        fixture_id, player_id, team_id,
                        to_int(league_item.get("id")) or league_id,
                        to_int(league_item.get("season")) or season,
                        to_text(player.get("type")),
                        to_text(player.get("reason")),
                    ),
                    "fixture_id, player_id",
                )
                ctx.items_processed += 1
    return ctx


# ---------------------------------------------------------- шаг 4: коэффициенты


def _fixture_ids_in_range(conn, start: date, end: date) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fixture_id FROM fixtures WHERE match_date BETWEEN %s AND %s",
            (start, end),
        )
        return {row[0] for row in cur.fetchall()}


def run_odds(client: ApiClient, conn, horizon_days: int = 3, bookmakers: set[str] | None = None) -> StepContext:
    """Снимок линии на ближайшие дни, только для настроенных букмекеров (ФТ-10)."""
    allowed = bookmakers if bookmakers is not None else DEFAULT_BOOKMAKERS
    taken_at = datetime.now(timezone.utc)
    today = taken_at.date()
    dates = [today + timedelta(days=i) for i in range(horizon_days)]
    known_fixtures = _fixture_ids_in_range(conn, dates[0], dates[-1])

    with collection_run(conn, client, "odds") as ctx:
        for day in dates:
            if client.quota_exhausted or client.requests_used >= client.max_requests:
                break
            # Кэш не используется — это критично: без этого второй снимок
            # в 12:00 вернул бы закэшированный ответ 00:00 с тем же параметром
            # date, и вся идея "видно движение линии" (ФТ-10) не работала бы.
            items = client.paged("/odds", {"date": day.isoformat()}, use_cache=False)
            for item in items:
                fixture_id = to_int((item.get("fixture") or {}).get("id"))
                if fixture_id is None or fixture_id not in known_fixtures:
                    ctx.skipped += 1
                    continue

                all_bookmakers = item.get("bookmakers") or []
                for bm in all_bookmakers:
                    bm_id = to_int(bm.get("id"))
                    if bm_id is not None:
                        write_row(conn, "bookmakers", ["bookmaker_id", "name"], (bm_id, bm.get("name")), "bookmaker_id")

                relevant = [bm for bm in all_bookmakers if bm.get("name") in allowed]
                if not relevant:
                    continue

                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO odds_snapshots (fixture_id, taken_at, source_updated_at)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (fixture_id, taken_at)
                           DO UPDATE SET source_updated_at = EXCLUDED.source_updated_at
                           RETURNING snapshot_id""",
                        (fixture_id, taken_at, to_utc(item.get("update"))),
                    )
                    snapshot_id = cur.fetchone()[0]

                value_rows = []
                for bm in relevant:
                    bm_id = to_int(bm.get("id"))
                    for bet in bm.get("bets") or []:
                        bet_id = to_int(bet.get("id"))
                        if bet_id is None:
                            continue
                        write_row(conn, "bet_types", ["bet_type_id", "name"], (bet_id, bet.get("name")), "bet_type_id")
                        for value in bet.get("values") or []:
                            odd = to_decimal(value.get("odd"))
                            outcome = to_text(value.get("value"))
                            if odd is None or outcome is None:
                                continue
                            value_rows.append((snapshot_id, bm_id, bet_id, outcome, odd))

                if value_rows:
                    write_batch(
                        conn, "odds_values",
                        ["snapshot_id", "bookmaker_id", "bet_type_id", "value", "odd"],
                        value_rows, "snapshot_id, bookmaker_id, bet_type_id, value",
                    )
                ctx.items_processed += 1
    return ctx


# ---------------------------------------------------- шаги 5-7: события/статистика/составы


def _process_events(client: ApiClient, conn, fixture_id: int) -> bool:
    """События одного матча. True — записаны, False — неудача (попытка учтена).

    Бросает Stop, когда бюджет клиента исчерпан (до каких-либо записей).
    """
    items = _fetch_items(client, "/fixtures/events", fixture_id)
    if items is None:
        mark_attempt(conn, fixture_id, "events")
        return False
    for event in items:
        team = event.get("team") or {}
        player = event.get("player") or {}
        assist = event.get("assist") or {}
        time_info = event.get("time") or {}
        team_id = to_int(team.get("id"))
        player_id = to_int(player.get("id"))
        _upsert_team(conn, team_id, team.get("name"))
        _upsert_player(conn, player_id, player.get("name"))
        assist_id = to_int(assist.get("id"))
        if assist_id is not None:
            _upsert_player(conn, assist_id, assist.get("name"))
        key = event_key(
            fixture_id, time_info.get("elapsed"), time_info.get("extra"),
            team_id, event.get("type"), event.get("detail"), player_id,
        )
        write_row(
            conn, "fixture_events",
            ["fixture_id", "team_id", "minute", "minute_extra", "type", "detail",
             "player_id", "assist_player_id", "comments", "event_key"],
            (
                fixture_id, team_id, to_int(time_info.get("elapsed")), to_int(time_info.get("extra")),
                to_text(event.get("type")) or "unknown", to_text(event.get("detail")),
                player_id, assist_id, to_text(event.get("comments")), key,
            ),
            "event_key",
        )
    mark_fetched(conn, fixture_id, "events")
    return True


def _parse_statistics_row(entry: dict) -> dict[str, int | float | None]:
    result: dict[str, int | float | None] = {}
    for row in entry.get("statistics") or []:
        stat_type = row.get("type")
        value = row.get("value")
        if stat_type in STAT_TYPE_MAP:
            result[STAT_TYPE_MAP[stat_type]] = to_int(value)
        elif stat_type in STAT_PERCENT_TYPES:
            result[STAT_PERCENT_TYPES[stat_type]] = to_percent(value)
        elif stat_type in STAT_DECIMAL_TYPES:
            result[STAT_DECIMAL_TYPES[stat_type]] = to_decimal(value)
    return result


STAT_COLUMNS = (
    list(STAT_TYPE_MAP.values()) + list(STAT_PERCENT_TYPES.values()) + list(STAT_DECIMAL_TYPES.values())
)


def _process_statistics(client: ApiClient, conn, fixture_id: int) -> bool:
    items = _fetch_items(client, "/fixtures/statistics", fixture_id)
    if items is None:
        mark_attempt(conn, fixture_id, "statistics")
        return False
    for entry in items:
        team = entry.get("team") or {}
        team_id = to_int(team.get("id"))
        if team_id is None:
            continue
        _upsert_team(conn, team_id, team.get("name"))
        values = _parse_statistics_row(entry)
        write_row(
            conn, "fixture_statistics",
            ["fixture_id", "team_id", *STAT_COLUMNS],
            (fixture_id, team_id, *[values.get(c) for c in STAT_COLUMNS]),
            "fixture_id, team_id",
        )
    mark_fetched(conn, fixture_id, "statistics")
    return True


def _process_lineups(client: ApiClient, conn, fixture_id: int) -> bool:
    items = _fetch_items(client, "/fixtures/lineups", fixture_id)
    if items is None:
        mark_attempt(conn, fixture_id, "lineups")
        return False
    for entry in items:
        team = entry.get("team") or {}
        coach = entry.get("coach") or {}
        team_id = to_int(team.get("id"))
        if team_id is None:
            continue
        _upsert_team(conn, team_id, team.get("name"))
        write_row(
            conn, "fixture_lineups",
            ["fixture_id", "team_id", "formation", "coach_id", "coach_name"],
            (fixture_id, team_id, to_text(entry.get("formation")), to_int(coach.get("id")), to_text(coach.get("name"))),
            "fixture_id, team_id",
        )
        for is_starter, group in ((True, entry.get("startXI")), (False, entry.get("substitutes"))):
            for slot in group or []:
                player = slot.get("player") or {}
                player_id = to_int(player.get("id"))
                if player_id is None:
                    continue
                _upsert_player(conn, player_id, player.get("name"))
                write_row(
                    conn, "fixture_lineup_players",
                    ["fixture_id", "team_id", "player_id", "is_starter", "shirt_number", "position", "grid"],
                    (fixture_id, team_id, player_id, is_starter, to_int(player.get("number")), to_text(player.get("pos")), to_text(player.get("grid"))),
                    "fixture_id, team_id, player_id",
                )
    mark_fetched(conn, fixture_id, "lineups")
    return True


KINDS = ("events", "statistics", "lineups")
_PROCESSORS = {"events": _process_events, "statistics": _process_statistics, "lineups": _process_lineups}
_COVERAGE = {"events": "has_events", "statistics": "has_statistics", "lineups": "has_lineups"}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _budget_left(client: ApiClient) -> bool:
    return not client.quota_exhausted and client.requests_used < client.max_requests


def _run_fresh(
    client: ApiClient, conn, kind: str, limit: int, fresh_days: int | None, today: date | None
) -> StepContext:
    """Шаги 5-7: только свежие матчи (ДЗ-1). `fresh_days=None` — без ограничения по дате."""
    min_date = None
    if fresh_days is not None:
        min_date = (today or _today()) - timedelta(days=fresh_days)
    with collection_run(conn, client, kind) as ctx:
        for fixture_id in _pending_fixtures(conn, kind, _COVERAGE[kind], limit, min_date=min_date):
            try:
                if _PROCESSORS[kind](client, conn, fixture_id):
                    ctx.items_processed += 1
            except Stop:
                break
    return ctx


def run_events(client: ApiClient, conn, limit: int = 20_000, fresh_days: int | None = FRESH_DAYS, today: date | None = None) -> StepContext:
    return _run_fresh(client, conn, "events", limit, fresh_days, today)


def run_statistics(client: ApiClient, conn, limit: int = 20_000, fresh_days: int | None = FRESH_DAYS, today: date | None = None) -> StepContext:
    return _run_fresh(client, conn, "statistics", limit, fresh_days, today)


def run_lineups(client: ApiClient, conn, limit: int = 20_000, fresh_days: int | None = FRESH_DAYS, today: date | None = None) -> StepContext:
    return _run_fresh(client, conn, "lineups", limit, fresh_days, today)


# ------------------------------------------------------------ шаг 8: дозагрузка


def run_backfill(
    client: ApiClient,
    conn,
    since: date = DEFAULT_BACKFILL_SINCE,
    fresh_days: int = FRESH_DAYS,
    today: date | None = None,
    chunk: int = BACKFILL_CHUNK,
) -> StepContext:
    """Дозагрузка пропущенного на остатке лимита (ФТ-11, specs/дозагрузка-пропущенного.md).

    Берёт матчи от `since` (включительно) до границы свежих (не включая): свежие
    обрабатывает ежедневный сбор. Остаток бюджета делится между событиями,
    статистикой и составами раундами по `chunk` матчей на набор (ДЗ-4); у набора
    с пустой очередью долю забирают остальные. Внутри набора — от новых к старым.
    """
    upper = (today or _today()) - timedelta(days=fresh_days)
    done = dict.fromkeys(KINDS, 0)
    with collection_run(conn, client, "backfill") as ctx:
        active = list(KINDS)
        while active and _budget_left(client):
            for kind in list(active):
                ids = _pending_fixtures(
                    conn, kind, _COVERAGE[kind], chunk, min_date=since, max_date=upper
                )
                if not ids:
                    active.remove(kind)
                    continue
                try:
                    for fixture_id in ids:
                        if _PROCESSORS[kind](client, conn, fixture_id):
                            done[kind] += 1
                            ctx.items_processed += 1
                except Stop:
                    active.clear()  # бюджет кончился — остановка целиком (ДЗ-10)
                    break
    print(
        f"дозагрузка: события {done['events']}, статистика {done['statistics']}, "
        f"составы {done['lineups']}"
    )
    return ctx


def backfill_report(
    conn,
    since: date = DEFAULT_BACKFILL_SINCE,
    fresh_days: int = FRESH_DAYS,
    today: date | None = None,
    avg_leftover: int = 5_500,
) -> dict:
    """Очередь по трём наборам: свежие / дозагрузка / глубокая история (ДЗ-9).

    Только читает базу. `avg_leftover` — средний остаток лимита в сутки для
    оценки срока. Возвращает словарь, чтобы отчёт можно было проверить тестом.
    """
    upper = (today or _today()) - timedelta(days=fresh_days)
    sql = """
        WITH pend AS (
            SELECT f.match_date,
                   (s.events_fetched_at IS NULL AND COALESCE(s.events_attempts, 0) < %(max)s
                        AND COALESCE(ls.has_events, true)) AS need_events,
                   (s.statistics_fetched_at IS NULL AND COALESCE(s.statistics_attempts, 0) < %(max)s
                        AND COALESCE(ls.has_statistics, true)) AS need_statistics,
                   (s.lineups_fetched_at IS NULL AND COALESCE(s.lineups_attempts, 0) < %(max)s
                        AND COALESCE(ls.has_lineups, true)) AS need_lineups
            FROM fixtures f
            JOIN leagues l ON l.league_id = f.league_id AND l.is_tracked
            LEFT JOIN league_seasons ls ON ls.league_id = f.league_id AND ls.season = f.season
            LEFT JOIN fixture_fetch_state s ON s.fixture_id = f.fixture_id
            WHERE f.status_short = ANY(%(finished)s)
        )
        SELECT CASE WHEN match_date >= %(upper)s THEN 'fresh'
                    WHEN match_date >= %(since)s THEN 'backfill'
                    ELSE 'deep' END AS grp,
               count(*) FILTER (WHERE need_events),
               count(*) FILTER (WHERE need_statistics),
               count(*) FILTER (WHERE need_lineups)
        FROM pend GROUP BY 1
    """
    params = {"max": MAX_ATTEMPTS, "finished": list(FINISHED_STATUSES), "upper": upper, "since": since}
    report = {g: dict.fromkeys(KINDS, 0) for g in ("fresh", "backfill", "deep")}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for grp, events, statistics, lineups in cur.fetchall():
            report[grp] = {"events": events, "statistics": statistics, "lineups": lineups}
    remaining = sum(report["backfill"].values())
    report["backfill_requests"] = remaining
    report["eta_days"] = -(-remaining // avg_leftover) if remaining else 0  # округление вверх
    return report


def print_backfill_report(conn, since: date, fresh_days: int, avg_leftover: int = 5_500) -> dict:
    report = backfill_report(conn, since, fresh_days, avg_leftover=avg_leftover)
    print(f"Очередь сбора (горизонт дозагрузки с {since}, свежие — последние {fresh_days} дн.)\n")
    print(f"{'группа':<28}{'события':>10}{'статистика':>12}{'составы':>10}")
    titles = {
        "fresh": "свежие (ежедневный сбор)",
        "backfill": "дозагрузка",
        "deep": f"глубокая история (до {since})",
    }
    for grp in ("fresh", "backfill", "deep"):
        r = report[grp]
        print(f"{titles[grp]:<28}{r['events']:>10}{r['statistics']:>12}{r['lineups']:>10}")
    print(
        f"\nДозагрузка: {report['backfill_requests']} запросов, "
        f"около {report['eta_days']} сут. при среднем остатке {avg_leftover} в сутки."
    )
    return report


# ---------------------------------------------------------------------- daily


def run_daily(
    client: ApiClient,
    conn,
    fresh_days: int = FRESH_DAYS,
    backfill: bool = True,
    backfill_since: date = DEFAULT_BACKFILL_SINCE,
    today: date | None = None,
) -> list[StepContext]:
    """Ежедневный сбор: матчи, травмы, свежие события/статистика/составы, дозагрузка.

    Порядок продиктован приоритетом (ДЗ-2): дозагрузка идёт последней и получает
    только то, что осталось от бюджета. Коэффициенты сюда НЕ входят: они идут
    отдельным запуском дважды в сутки (ФТ-2, ФТ-10), иначе ежедневный сбор делал бы
    третий снимок линии. Один клиент — общий бюджет запросов на все шаги.
    """
    steps = [
        lambda: run_fixtures(client, conn),
        lambda: run_injuries(client, conn),
        lambda: run_events(client, conn, fresh_days=fresh_days, today=today),
        lambda: run_statistics(client, conn, fresh_days=fresh_days, today=today),
        lambda: run_lineups(client, conn, fresh_days=fresh_days, today=today),
    ]
    if backfill:
        steps.append(
            lambda: run_backfill(
                client, conn, since=backfill_since, fresh_days=fresh_days, today=today
            )
        )
    results = []
    for step in steps:
        results.append(step())
        if client.quota_exhausted:
            break
    return results


# ---------------------------------------------------------------------- CLI


JOBS = {
    "catalog": lambda client, conn, args: run_catalog(client, conn),
    "fixtures": lambda client, conn, args: run_fixtures(client, conn),
    "injuries": lambda client, conn, args: run_injuries(client, conn),
    "odds": lambda client, conn, args: run_odds(client, conn, horizon_days=args.odds_horizon_days),
    "events": lambda client, conn, args: run_events(client, conn, fresh_days=args.fresh_days),
    "statistics": lambda client, conn, args: run_statistics(client, conn, fresh_days=args.fresh_days),
    "lineups": lambda client, conn, args: run_lineups(client, conn, fresh_days=args.fresh_days),
    "backfill": lambda client, conn, args: run_backfill(
        client, conn, since=args.backfill_since, fresh_days=args.fresh_days
    ),
    "daily": lambda client, conn, args: run_daily(
        client,
        conn,
        fresh_days=args.fresh_days,
        backfill=not args.no_backfill,
        backfill_since=args.backfill_since,
    ),
}


def main(argv: list[str] | None = None) -> int:
    import os
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Ежедневный сбор данных API-Football")
    parser.add_argument("--job", required=True, choices=sorted(JOBS) + ["cleanup", "backfill-report"])
    parser.add_argument("--max-requests", type=int, default=7_000)
    parser.add_argument("--odds-horizon-days", type=int, default=3)
    parser.add_argument("--fresh-days", type=int, default=FRESH_DAYS)
    parser.add_argument("--backfill-since", type=date.fromisoformat, default=DEFAULT_BACKFILL_SINCE)
    parser.add_argument("--no-backfill", action="store_true", help="daily без дозагрузки")
    parser.add_argument("--avg-leftover", type=int, default=5_500, help="для оценки срока в отчёте")
    parser.add_argument("--cache-dir", type=Path, default=Path("cache"))
    args = parser.parse_args(argv)

    if args.job == "cleanup":
        # Только удаление старых файлов кэша (ADR-2): ни ключ, ни база не нужны.
        removed = ApiClient(key="", cache_dir=args.cache_dir, verbose=False).cleanup_cache(30)
        print(f"удалено файлов кэша старше 30 дней: {removed}")
        return 0

    if args.job == "backfill-report":
        # Только чтение базы: ключ API и запросы не нужны (ДЗ-9).
        with connect(database_url()) as conn:
            print_backfill_report(conn, args.backfill_since, args.fresh_days, args.avg_leftover)
        return 0

    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        print("Не задана переменная окружения APIFOOTBALL_KEY", file=sys.stderr)
        return 1

    client = ApiClient(key=key, cache_dir=args.cache_dir, max_requests=args.max_requests)
    with connect(database_url()) as conn:
        result = JOBS[args.job](client, conn, args)
        contexts = result if isinstance(result, list) else [result]
        for ctx in contexts:
            print(f"обработано: {ctx.items_processed}, пропущено: {ctx.skipped}")
        print(f"запросов потрачено: {client.requests_used}, остаток по API: {client.daily_remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
