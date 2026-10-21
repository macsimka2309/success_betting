"""Сбор баскетбольных данных (specs/баскетбол-сбор-данных.md).

Все таблицы в схеме `basketball`. Те же принципы, что у футбольного сбора: естественные
ключи источника и запись через ON CONFLICT (повторный запуск безопасен), кэш ответов на
диске, журнал запусков в collection_runs, максимум 3 попытки на набор данных игры.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.api.client import ApiClient
from src.common.convert import to_int, to_utc
from src.common.db import write_batch, write_row
from src.db.connection import connect, database_url
from src.jobs.collect import Stop, collection_run

BASE_URL = "https://v1.basketball.api-sports.io"
FINISHED = ("FT", "AOT")
MAX_ATTEMPTS = 3
SINCE_YEAR = 2020
STATS_SINCE = date(2020, 1, 1)

BOOKMAKERS = ("Pinnacle", "1xBet")
# id рынков API-Basketball: победитель матча, тотал матча, тоталы таймов, четвертей, овертайма.
MARKET_HOME_AWAY = 2
MARKET_TOTALS = {4, 5, 31, 16, 45, 46, 47, 97}
MARKETS = MARKET_TOTALS | {MARKET_HOME_AWAY}
LADDER_NEIGHBOURS = 2  # основная линия и по 2 ближайших с каждой стороны
ODDS_HORIZON_HOURS = 36


# ------------------------------------------------------------------ разбор


SMALLINT_MAX = 32767


def to_small(value) -> int | None:
    """Целое для колонки SMALLINT. Значение вне диапазона — ошибка источника (замечено:
    1 464 578 попыток бросков в одной игре) — хранится как NULL, чтобы не ронять загрузку."""
    number = to_int(value)
    if number is None or abs(number) > SMALLINT_MAX:
        return None
    return number


def season_year(season: str) -> int:
    """`2025-2026` и `2025` -> 2025."""
    return int(str(season)[:4])


def parse_minutes(value) -> int | None:
    """`"32:17"` -> 1937 секунд; пусто и нечисловое -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        head, _, tail = text.partition(":")
        try:
            return int(head) * 60 + int(tail)
        except ValueError:
            return None
    try:
        return int(float(text)) * 60
    except ValueError:
        return None


def parse_league(item: dict) -> tuple[dict, list[dict]]:
    country = item.get("country") or {}
    league = {
        "league_id": to_int(item.get("id")),
        "name": str(item.get("name") or ""),
        "type": item.get("type"),
        "country": country.get("name"),
        "country_code": country.get("code"),
    }
    seasons = []
    for season in item.get("seasons") or []:
        coverage = season.get("coverage") or {}
        stats = (coverage.get("games") or {}).get("statistics") or {}
        seasons.append({
            "league_id": league["league_id"],
            "season": str(season.get("season")),
            "start_date": season.get("start") or None,
            "end_date": season.get("end") or None,
            "has_team_stats": bool(stats.get("teams")),
            "has_player_stats": bool(stats.get("players")),
            "has_odds": bool(coverage.get("odds")),
            "has_standings": bool(coverage.get("standings")),
        })
    return league, seasons


GAME_COLUMNS = (
    "game_id", "league_id", "season", "tip_off", "game_date", "stage", "week", "status_short",
    "status_long", "home_team_id", "away_team_id",
    "home_q1", "home_q2", "home_q3", "home_q4", "home_ot", "home_total",
    "away_q1", "away_q2", "away_q3", "away_q4", "away_ot", "away_total", "venue",
)


def parse_game(item: dict) -> dict | None:
    """None, если нет обязательных данных (игра без команд или времени)."""
    teams = item.get("teams") or {}
    home, away = teams.get("home") or {}, teams.get("away") or {}
    tip_off = to_utc(item.get("date"))
    league = item.get("league") or {}
    status = item.get("status") or {}
    game_id = to_int(item.get("id"))
    home_id, away_id = to_int(home.get("id")), to_int(away.get("id"))
    if None in (game_id, home_id, away_id, tip_off) or not status.get("short") or league.get("id") is None:
        return None
    scores = item.get("scores") or {}

    def side(name: str) -> list[int | None]:
        score = scores.get(name) or {}
        return [to_small(score.get(k)) for k in ("quarter_1", "quarter_2", "quarter_3", "quarter_4", "over_time", "total")]

    row = {
        "game_id": game_id,
        "league_id": to_int(league.get("id")),
        "season": str(league.get("season")),
        "tip_off": tip_off,
        "game_date": tip_off.date(),
        "stage": item.get("stage"),
        "week": item.get("week"),
        "status_short": status["short"],
        "status_long": status.get("long"),
        "home_team_id": home_id,
        "away_team_id": away_id,
        "venue": item.get("venue"),
        "_home_name": home.get("name"),
        "_away_name": away.get("name"),
        "_league_name": league.get("name"),
        "_league_type": league.get("type"),
    }
    for prefix, values in (("home", side("home")), ("away", side("away"))):
        for key, value in zip(("q1", "q2", "q3", "q4", "ot", "total"), values):
            row[f"{prefix}_{key}"] = value
    return row


def _shot(entry: dict, key: str) -> tuple[int | None, int | None]:
    block = entry.get(key) or {}
    return to_small(block.get("total")), to_small(block.get("attempts"))


TEAM_STAT_COLUMNS = (
    "fg_made", "fg_attempts", "three_made", "three_attempts", "ft_made", "ft_attempts",
    "reb_total", "reb_offence", "reb_defense", "assists", "steals", "blocks", "turnovers", "personal_fouls",
)


def parse_team_stats(entry: dict) -> dict:
    fg = _shot(entry, "field_goals")
    three = _shot(entry, "threepoint_goals")
    ft = _shot(entry, "freethrows_goals")
    reb = entry.get("rebounds") or {}
    return {
        "fg_made": fg[0], "fg_attempts": fg[1], "three_made": three[0], "three_attempts": three[1],
        "ft_made": ft[0], "ft_attempts": ft[1],
        "reb_total": to_small(reb.get("total")), "reb_offence": to_small(reb.get("offence")),
        "reb_defense": to_small(reb.get("defense")), "assists": to_small(entry.get("assists")),
        "steals": to_small(entry.get("steals")), "blocks": to_small(entry.get("blocks")),
        "turnovers": to_small(entry.get("turnovers")), "personal_fouls": to_small(entry.get("personal_fouls")),
    }


PLAYER_STAT_COLUMNS = (
    "is_starter", "seconds_played", "points", "fg_made", "fg_attempts", "three_made", "three_attempts",
    "ft_made", "ft_attempts", "reb_total", "assists",
)


def parse_player_stats(entry: dict) -> dict:
    fg = _shot(entry, "field_goals")
    three = _shot(entry, "threepoint_goals")
    ft = _shot(entry, "freethrows_goals")
    kind = entry.get("type")
    return {
        "is_starter": None if kind is None else kind == "starters",
        "seconds_played": parse_minutes(entry.get("minutes")),
        "points": to_small(entry.get("points")),
        "fg_made": fg[0], "fg_attempts": fg[1], "three_made": three[0], "three_attempts": three[1],
        "ft_made": ft[0], "ft_attempts": ft[1],
        "reb_total": to_small((entry.get("rebounds") or {}).get("total")),
        "assists": to_small(entry.get("assists")),
    }


_VALUE = re.compile(r"^(Over|Under)\s+(\d+(?:\.\d+)?)$")


def _ladder(rows: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """Основная линия тотала и по LADDER_NEIGHBOURS ближайших с каждой стороны.

    Основная — линия, где коэффициенты Over и Under ближе всего друг к другу.
    """
    by_line: dict[float, dict[str, float]] = {}
    for outcome, line, odd in rows:
        by_line.setdefault(line, {})[outcome] = odd
    lines = sorted(by_line)
    if not lines:
        return []
    paired = [ln for ln in lines if "Over" in by_line[ln] and "Under" in by_line[ln]]
    if paired:
        main = min(paired, key=lambda ln: (abs(by_line[ln]["Over"] - by_line[ln]["Under"]), ln))
    else:
        main = lines[len(lines) // 2]
    index = lines.index(main)
    keep = set(lines[max(0, index - LADDER_NEIGHBOURS): index + LADDER_NEIGHBOURS + 1])
    return [r for r in rows if r[1] in keep]


def parse_odds(item: dict) -> list[tuple[int, str, int, str, str, float, float]]:
    """[(bookmaker_id, bookmaker, bet_type_id, bet_name, outcome, line, odd)] только Pinnacle и 1xBet."""
    result = []
    for bookmaker in item.get("bookmakers") or []:
        if bookmaker.get("name") not in BOOKMAKERS:
            continue
        for bet in bookmaker.get("bets") or []:
            bet_id = to_int(bet.get("id"))
            if bet_id not in MARKETS:
                continue
            rows: list[tuple[str, float, float]] = []
            for value in bet.get("values") or []:
                try:
                    odd = float(str(value.get("odd")).strip())
                except (TypeError, ValueError):
                    continue
                text = str(value.get("value") or "").strip()
                if bet_id == MARKET_HOME_AWAY:
                    if text in ("Home", "Away"):
                        rows.append((text, 0.0, odd))
                    continue
                match = _VALUE.match(text)
                if match:
                    rows.append((match.group(1), float(match.group(2)), odd))
            if bet_id in MARKET_TOTALS:
                rows = _ladder(rows)
            for outcome, line, odd in rows:
                result.append((to_int(bookmaker.get("id")), bookmaker["name"], bet_id, str(bet.get("name")), outcome, line, odd))
    return result


# --------------------------------------------------------------------- шаги


def _fetch_list(client: ApiClient, endpoint: str, params: dict, use_cache: bool) -> list | None:
    """Список `response`, None при неудаче; Stop — бюджет или суточный лимит исчерпаны."""
    body = client.get(endpoint, params, use_cache=use_cache)
    if body is None:
        if client.quota_exhausted or client.requests_used >= client.max_requests:
            raise Stop()
        return None
    if body.get("errors"):
        return None
    return body.get("response") or []


def run_catalog(client: ApiClient, conn):
    """Лиги и сезоны с флагами покрытия."""
    with collection_run(conn, client, "bb_catalog") as ctx:
        try:
            items = _fetch_list(client, "/leagues", {}, use_cache=False)
        except Stop:
            return ctx
        for item in items or []:
            league, seasons = parse_league(item)
            if league["league_id"] is None:
                ctx.skipped += 1
                continue
            write_row(conn, "basketball.leagues", list(league), tuple(league.values()), "league_id")
            if seasons:
                cols = list(seasons[0])
                write_batch(conn, "basketball.league_seasons", cols, [tuple(s[c] for c in cols) for s in seasons],
                            "league_id, season")
            ctx.items_processed += 1
    return ctx


def _today() -> date:
    return datetime.now(timezone.utc).date()


def season_pairs(conn, since_year: int, all_seasons: bool, today: date) -> list[tuple[int, str]]:
    """Пары (лига, сезон) для загрузки списков игр: от новых сезонов к старым.

    Без all_seasons — только идущие и недавно закончившиеся (даты известны).
    """
    where = ""
    params: list = []
    if not all_seasons:
        where = " AND ls.start_date <= %s AND ls.end_date >= %s"
        params = [today + timedelta(days=30), today - timedelta(days=7)]
    sql = f"""
        SELECT ls.league_id, ls.season FROM basketball.league_seasons ls
        WHERE left(ls.season, 4)::int >= %s{where}
        ORDER BY left(ls.season, 4)::int DESC, ls.league_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, [since_year, *params])
        return [(row[0], row[1]) for row in cur.fetchall()]


def _store_games(conn, items: list) -> tuple[int, int]:
    saved = skipped = 0
    games = []
    for item in items:
        game = parse_game(item)
        if game is None:
            skipped += 1
            continue
        games.append(game)
    if not games:
        return 0, skipped
    leagues = {g["league_id"]: (g["league_id"], g["_league_name"] or f"Лига {g['league_id']}", g["_league_type"])
               for g in games}
    write_batch(conn, "basketball.leagues", ["league_id", "name", "type"], list(leagues.values()), "league_id",
                update_columns=[])
    teams: dict[int, tuple] = {}
    for g in games:
        teams[g["home_team_id"]] = (g["home_team_id"], g["_home_name"] or f"Команда {g['home_team_id']}")
        teams[g["away_team_id"]] = (g["away_team_id"], g["_away_name"] or f"Команда {g['away_team_id']}")
    write_batch(conn, "basketball.teams", ["team_id", "name"], list(teams.values()), "team_id")
    write_batch(conn, "basketball.games", list(GAME_COLUMNS), [tuple(g[c] for c in GAME_COLUMNS) for g in games],
                "game_id")
    return len(games), skipped


def run_games(client: ApiClient, conn, since_year: int = SINCE_YEAR, all_seasons: bool = False, today: date | None = None):
    """Списки игр: один запрос на пару лига-сезон."""
    with collection_run(conn, client, "bb_games") as ctx:
        for league_id, season in season_pairs(conn, since_year, all_seasons, today or _today()):
            try:
                items = _fetch_list(client, "/games", {"league": league_id, "season": season}, use_cache=False)
            except Stop:
                break
            if items is None:
                ctx.skipped += 1
                continue
            saved, skipped = _store_games(conn, items)
            ctx.items_processed += saved
            ctx.skipped += skipped
    return ctx


def _mark(conn, game_id: int, kind: str, fetched: bool) -> None:
    fetched_col, attempts_col = f"{kind}_stats_fetched_at", f"{kind}_stats_attempts"
    if fetched:
        sql = (f"INSERT INTO basketball.fetch_state (game_id, {fetched_col}) VALUES (%s, now()) "
               f"ON CONFLICT (game_id) DO UPDATE SET {fetched_col} = now()")
    else:
        sql = (f"INSERT INTO basketball.fetch_state (game_id, {attempts_col}) VALUES (%s, 1) "
               f"ON CONFLICT (game_id) DO UPDATE SET {attempts_col} = basketball.fetch_state.{attempts_col} + 1")
    with conn.cursor() as cur:
        cur.execute(sql, (game_id,))


def pending_stats(conn, limit: int, since: date = STATS_SINCE) -> list[tuple[int, bool, bool]]:
    """Завершённые игры без статистики: от свежих к старым. [(game_id, нужны команды, нужны игроки)]."""
    sql = """
        SELECT g.game_id,
               (s.team_stats_fetched_at IS NULL AND COALESCE(s.team_stats_attempts, 0) < %(max)s),
               (s.player_stats_fetched_at IS NULL AND COALESCE(s.player_stats_attempts, 0) < %(max)s)
        FROM basketball.games g
        LEFT JOIN basketball.fetch_state s ON s.game_id = g.game_id
        WHERE g.status_short = ANY(%(finished)s) AND g.game_date >= %(since)s
          AND ((s.team_stats_fetched_at IS NULL AND COALESCE(s.team_stats_attempts, 0) < %(max)s)
            OR (s.player_stats_fetched_at IS NULL AND COALESCE(s.player_stats_attempts, 0) < %(max)s))
        ORDER BY g.game_date DESC, g.game_id
        LIMIT %(limit)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"max": MAX_ATTEMPTS, "finished": list(FINISHED), "since": since, "limit": limit})
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def _process_team_stats(client: ApiClient, conn, game_id: int) -> bool:
    items = _fetch_list(client, "/games/statistics/teams", {"id": game_id}, use_cache=True)
    if items is None:
        _mark(conn, game_id, "team", fetched=False)
        return False
    teams, rows = [], []
    for entry in items:
        team_id = to_int((entry.get("team") or {}).get("id"))
        if team_id is None:
            continue
        teams.append((team_id, f"Команда {team_id}"))
        values = parse_team_stats(entry)
        rows.append((game_id, team_id, *[values[c] for c in TEAM_STAT_COLUMNS]))
    write_batch(conn, "basketball.teams", ["team_id", "name"], teams, "team_id", update_columns=[])
    write_batch(conn, "basketball.team_game_stats", ["game_id", "team_id", *TEAM_STAT_COLUMNS], rows,
                "game_id, team_id")
    _mark(conn, game_id, "team", fetched=True)
    return True


def _process_player_stats(client: ApiClient, conn, game_id: int) -> bool:
    items = _fetch_list(client, "/games/statistics/players", {"id": game_id}, use_cache=True)
    if items is None:
        _mark(conn, game_id, "player", fetched=False)
        return False
    teams, players, rows = {}, {}, []
    for entry in items:
        team_id = to_int((entry.get("team") or {}).get("id"))
        player = entry.get("player") or {}
        player_id = to_int(player.get("id"))
        if team_id is None or player_id is None:
            continue
        teams[team_id] = (team_id, f"Команда {team_id}")
        players[player_id] = (player_id, str(player.get("name") or f"Игрок {player_id}"))
        values = parse_player_stats(entry)
        rows.append((game_id, team_id, player_id, *[values[c] for c in PLAYER_STAT_COLUMNS]))
    write_batch(conn, "basketball.teams", ["team_id", "name"], list(teams.values()), "team_id", update_columns=[])
    write_batch(conn, "basketball.players", ["player_id", "name"], list(players.values()), "player_id")
    write_batch(conn, "basketball.player_game_stats", ["game_id", "team_id", "player_id", *PLAYER_STAT_COLUMNS],
                rows, "game_id, team_id, player_id")
    _mark(conn, game_id, "player", fetched=True)
    return True


def run_stats(client: ApiClient, conn, since: date = STATS_SINCE, chunk: int = 200):
    """Статистика команд и игроков для завершённых игр, от свежих к старым. Запросы идут до исчерпания бюджета."""
    with collection_run(conn, client, "bb_stats") as ctx:
        while client.requests_used < client.max_requests and not client.quota_exhausted:
            batch = pending_stats(conn, chunk, since)
            if not batch:
                break
            try:
                for game_id, need_team, need_player in batch:
                    ok = True
                    if need_team:
                        ok = _process_team_stats(client, conn, game_id) and ok
                    if need_player:
                        ok = _process_player_stats(client, conn, game_id) and ok
                    if ok:
                        ctx.items_processed += 1
            except Stop:
                break
    return ctx


def odds_game_ids(conn, now: datetime, horizon_hours: int = ODDS_HORIZON_HOURS) -> list[int]:
    sql = """
        SELECT g.game_id FROM basketball.games g
        JOIN basketball.league_seasons ls ON ls.league_id = g.league_id AND ls.season = g.season AND ls.has_odds
        WHERE g.status_short = 'NS' AND g.tip_off >= %s AND g.tip_off < %s
        ORDER BY g.tip_off, g.game_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (now, now + timedelta(hours=horizon_hours)))
        return [row[0] for row in cur.fetchall()]


def run_odds(client: ApiClient, conn, now: datetime | None = None):
    """Снимок коэффициентов Pinnacle и 1xBet на тоталы и победителя для ближайших игр."""
    now = now or datetime.now(timezone.utc)
    with collection_run(conn, client, "bb_odds") as ctx:
        for game_id in odds_game_ids(conn, now):
            try:
                items = _fetch_list(client, "/odds", {"game": game_id}, use_cache=False)
            except Stop:
                break
            rows = []
            for item in items or []:
                rows.extend(parse_odds(item))
            if not rows:
                ctx.skipped += 1
                continue
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO basketball.odds_snapshots (game_id) VALUES (%s) RETURNING snapshot_id", (game_id,))
                    snapshot_id = cur.fetchone()[0]
                    for bookmaker_id, name, bet_id, bet_name, outcome, line, odd in rows:
                        cur.execute("INSERT INTO basketball.bookmakers (bookmaker_id, name) VALUES (%s, %s) "
                                    "ON CONFLICT (bookmaker_id) DO UPDATE SET name = EXCLUDED.name", (bookmaker_id, name))
                        cur.execute("INSERT INTO basketball.bet_types (bet_type_id, name) VALUES (%s, %s) "
                                    "ON CONFLICT (bet_type_id) DO UPDATE SET name = EXCLUDED.name", (bet_id, bet_name))
                        cur.execute(
                            "INSERT INTO basketball.odds_values (snapshot_id, bookmaker_id, bet_type_id, outcome, line, odd) "
                            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                            (snapshot_id, bookmaker_id, bet_id, outcome, line, odd),
                        )
            ctx.items_processed += 1
    return ctx


def completeness_report(conn, months: int = 12) -> list[tuple]:
    """По месяцам: завершённых игр с 2020 года, со статистикой команд и игроков."""
    sql = """
        SELECT date_trunc('month', g.game_date)::date AS month, count(*) AS finished,
               count(*) FILTER (WHERE s.team_stats_fetched_at IS NOT NULL AND EXISTS (
                   SELECT 1 FROM basketball.team_game_stats t WHERE t.game_id = g.game_id)) AS with_team_stats,
               count(*) FILTER (WHERE s.player_stats_fetched_at IS NOT NULL AND EXISTS (
                   SELECT 1 FROM basketball.player_game_stats p WHERE p.game_id = g.game_id)) AS with_player_stats,
               count(*) FILTER (WHERE s.team_stats_fetched_at IS NULL OR s.player_stats_fetched_at IS NULL) AS pending
        FROM basketball.games g LEFT JOIN basketball.fetch_state s ON s.game_id = g.game_id
        WHERE g.status_short = ANY(%s) AND g.game_date >= %s
        GROUP BY 1 ORDER BY 1 DESC LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (list(FINISHED), STATS_SINCE, months))
        return cur.fetchall()


def print_report(conn) -> None:
    print(f"{'месяц':<12}{'игр':>8}{'команды':>10}{'игроки':>10}{'в очереди':>11}")
    for month, finished, teams, players, pending in completeness_report(conn):
        print(f"{month!s:<12}{finished:>8}{teams:>10}{players:>10}{pending:>11}")


JOBS = {"catalog": run_catalog, "games": run_games, "stats": run_stats, "odds": run_odds}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сбор данных API-Basketball")
    parser.add_argument("--job", required=True, choices=sorted(JOBS) + ["report"])
    parser.add_argument("--max-requests", type=int, default=50_000)
    parser.add_argument("--since-year", type=int, default=SINCE_YEAR)
    parser.add_argument("--all-seasons", action="store_true", help="games: все сезоны, а не только активные")
    parser.add_argument("--cache-dir", type=Path, default=Path("cache_bb"))
    args = parser.parse_args(argv)

    url = database_url()
    if args.job == "report":
        with connect(url) as conn:
            print_report(conn)
        return 0
    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        print("Не задана переменная окружения APIFOOTBALL_KEY", file=sys.stderr)
        return 1
    client = ApiClient(key=key, cache_dir=args.cache_dir, max_requests=args.max_requests,
                       base_url=BASE_URL, requests_per_minute=400, verbose=False)
    try:
        with connect(url) as conn:
            if args.job == "games":
                ctx = run_games(client, conn, since_year=args.since_year, all_seasons=args.all_seasons)
            elif args.job == "stats":
                ctx = run_stats(client, conn)
            else:
                ctx = JOBS[args.job](client, conn)
    except Exception as exc:
        print(f"ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"обработано: {ctx.items_processed}, пропущено: {ctx.skipped}, запросов: {client.requests_used}, "
          f"остаток по API: {client.daily_remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
