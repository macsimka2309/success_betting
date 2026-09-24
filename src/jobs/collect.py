"""Ежедневный сбор: семь шагов из docs/02-архитектура.md.

Спецификация: specs/ежедневный-сбор.md

Стадия 1 (скачивание в кэш) делает ApiClient. Этот модуль — стадия 2
(ADR-2): разбор ответов из кэша/API в строки таблиц и запись с защитой
от дублей. Каждый шаг фиксирует себя в collection_runs (ФТ-6) и уважает
общий бюджет запросов клиента (ФТ-5).

Использование:
    python3 -m src.jobs.collect --job catalog
    python3 -m src.jobs.collect --job daily   # шаги 2-7 подряд
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


def _pending_fixtures(conn, field_name: str, coverage_column: str, limit: int) -> list[int]:
    """Матчи, где данных ещё нет, а источник их отдаёт (ЕС-4, ЕС-5)."""
    fetched_column = _FETCHED_COLUMN[field_name]
    attempt_column = _ATTEMPT_COLUMN[field_name]
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
          ))
        ORDER BY f.match_date DESC, f.fixture_id
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (list(FINISHED_STATUSES), MAX_ATTEMPTS, limit))
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


def run_events(client: ApiClient, conn, limit: int = 20_000) -> StepContext:
    with collection_run(conn, client, "events") as ctx:
        fixture_ids = _pending_fixtures(conn, "events", "has_events", limit)
        for fixture_id in fixture_ids:
            try:
                items = _fetch_items(client, "/fixtures/events", fixture_id)
            except Stop:
                break
            if items is None:
                mark_attempt(conn, fixture_id, "events")
                continue
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
            ctx.items_processed += 1
    return ctx


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


def run_statistics(client: ApiClient, conn, limit: int = 20_000) -> StepContext:
    with collection_run(conn, client, "statistics") as ctx:
        fixture_ids = _pending_fixtures(conn, "statistics", "has_statistics", limit)
        for fixture_id in fixture_ids:
            try:
                items = _fetch_items(client, "/fixtures/statistics", fixture_id)
            except Stop:
                break
            if items is None:
                mark_attempt(conn, fixture_id, "statistics")
                continue
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
            ctx.items_processed += 1
    return ctx


def run_lineups(client: ApiClient, conn, limit: int = 20_000) -> StepContext:
    with collection_run(conn, client, "lineups") as ctx:
        fixture_ids = _pending_fixtures(conn, "lineups", "has_lineups", limit)
        for fixture_id in fixture_ids:
            try:
                items = _fetch_items(client, "/fixtures/lineups", fixture_id)
            except Stop:
                break
            if items is None:
                mark_attempt(conn, fixture_id, "lineups")
                continue
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
            ctx.items_processed += 1
    return ctx


# ---------------------------------------------------------------------- daily


def run_daily(client: ApiClient, conn) -> list[StepContext]:
    """Шаги 2-7 подряд, один клиент — общий бюджет (docs/02)."""
    steps = [run_fixtures, run_injuries, run_odds, run_events, run_statistics, run_lineups]
    results = []
    for step in steps:
        results.append(step(client, conn))
        if client.quota_exhausted:
            break
    return results


# ---------------------------------------------------------------------- CLI


JOBS = {
    "catalog": lambda client, conn, args: run_catalog(client, conn),
    "fixtures": lambda client, conn, args: run_fixtures(client, conn),
    "injuries": lambda client, conn, args: run_injuries(client, conn),
    "odds": lambda client, conn, args: run_odds(client, conn, horizon_days=args.odds_horizon_days),
    "events": lambda client, conn, args: run_events(client, conn),
    "statistics": lambda client, conn, args: run_statistics(client, conn),
    "lineups": lambda client, conn, args: run_lineups(client, conn),
    "daily": lambda client, conn, args: run_daily(client, conn),
}


def main(argv: list[str] | None = None) -> int:
    import os
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Ежедневный сбор данных API-Football")
    parser.add_argument("--job", required=True, choices=sorted(JOBS) + ["cleanup"])
    parser.add_argument("--max-requests", type=int, default=7_000)
    parser.add_argument("--odds-horizon-days", type=int, default=3)
    parser.add_argument("--cache-dir", type=Path, default=Path("cache"))
    args = parser.parse_args(argv)

    if args.job == "cleanup":
        # Только удаление старых файлов кэша (ADR-2): ни ключ, ни база не нужны.
        removed = ApiClient(key="", cache_dir=args.cache_dir, verbose=False).cleanup_cache(30)
        print(f"удалено файлов кэша старше 30 дней: {removed}")
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
