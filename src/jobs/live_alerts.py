"""Оповещения по идущим матчам (specs/онлайн-оповещения.md).

Каждый запуск: один запрос `fixtures?live=all`, отбор кандидатов по минуте,
затем статистика кандидатов пачками `fixtures?ids=…` (до 20 матчей). Две категории:
`stats` (владение и удары в створ за матч) и `half` (владение во втором тайме больше 70 %).
Каждая категория уходит в Telegram по матчу один раз. Снимки матчей не сохраняются,
кроме владения на перерыве (`live_ht_possession`) — оно нужно для оценки второго тайма.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from src.api.client import ApiClient
from src.common.convert import to_int, to_percent, to_utc
from src.db.connection import connect, database_url
from src.jobs.collect import collection_run

LIVE_STATUSES = ("1H", "2H")
MINUTE_FROM, MINUTE_TO = 60, 75
POSSESSION_ABOVE = 55
SHOTS_ON_GOAL_MIN = 6
HALF_POSSESSION_MIN = 70  # владение во втором тайме не меньше (с 18.10.2026; раньше строго больше)
HALF_MINUTE_FROM = 65
HALF_MAX_LEAD = 1  # команда проигрывает, играет вничью или ведёт не больше чем на мяч
HALF_PREFILTER = 62  # оценка второго тайма выше этого порога → точный запрос по таймам
ATTACK_POSSESSION_ABOVE = 55  # категория half_attack: владение во втором тайме строго больше
ATTACK_SHOTS_MIN = 4  # категория half_attack: удары в створ во втором тайме не меньше
ATTACK_PREFILTER_POSSESSION = 48  # оценка владения для отбора кандидатов half_attack
FIRST_HALF_MINUTES = 45
ENABLED_CATEGORIES = ("favorite", "half")  # stats и half_attack отключены 18.10.2026
LEGACY_CATEGORIES = ("stats", "half", "half_attack")
FAVORITE_MINUTE_FROM, FAVORITE_MINUTE_TO = 65, 75
FAVORITE_SHARE = 0.2  # верхние и нижние 20 % таблицы
MIN_TEAMS = 8
MIN_PLAYED = 5
IDS_BATCH = 20
DEFAULT_MAX_REQUESTS = 30
CATEGORY_TITLES = {
    "stats": "владение и удары в створ",
    "half": "владение во 2-м тайме",
    "half_attack": "атака во 2-м тайме",
    "favorite": "фаворит дома против аутсайдера",
}

Sender = Callable[[str], None]


class TelegramError(RuntimeError):
    """Ошибка отправки. Текст не содержит адреса запроса: в нём токен бота."""


@dataclass(frozen=True)
class Alert:
    fixture_id: int
    league_id: int | None
    league: str
    country: str
    home: str
    away: str
    minute: int
    goals_home: int
    goals_away: int
    team_side: str  # 'home' или 'away': команда, по которой сработал триггер
    possession: int  # для категории half — владение во втором тайме
    shots: int | None
    opponent_shots: int | None
    kickoff_at: object = None  # datetime | None
    alert_id: int | None = None  # номер оповещения; None в режиме dry-run
    category: str = "stats"
    total_possession: int | None = None  # для категории half: владение за матч
    home_rank: int | None = None  # категория favorite: места в таблице
    away_rank: int | None = None
    teams_count: int | None = None
    home_points: int | None = None  # только для сообщения, в базе не хранятся
    away_points: int | None = None

    @property
    def team(self) -> str:
        return self.home if self.team_side == "home" else self.away


# ------------------------------------------------------------------- триггер


def _score_and_minute(item: dict) -> tuple[int, int, int] | None:
    status = (item.get("fixture") or {}).get("status") or {}
    if status.get("short") not in LIVE_STATUSES:
        return None
    minute = to_int(status.get("elapsed"))
    goals = item.get("goals") or {}
    home, away = to_int(goals.get("home")), to_int(goals.get("away"))
    if minute is None or home is None or away is None:
        return None
    return minute, home, away


def is_candidate(item: dict) -> bool:
    """Т-1: идущий тайм и минута 60–75. Счёт не фильтруется (убрано 15.10.2026)."""
    parsed = _score_and_minute(item)
    if parsed is None:
        return False
    return MINUTE_FROM <= parsed[0] <= MINUTE_TO


def _team_stats(item: dict, team_id: int | None) -> dict[str, object]:
    for entry in item.get("statistics") or []:
        if to_int((entry.get("team") or {}).get("id")) == team_id:
            return {row.get("type"): row.get("value") for row in entry.get("statistics") or []}
    return {}


def build_alert(item: dict) -> Alert | None:
    """Триггер Т-1, Т-3, Т-4 по ответу `ids=` для любой из двух команд.

    Владение больше 55 % возможно только у одной команды, поэтому оповещение
    по матчу не больше одного. None — матч не подходит или данных нет.
    """
    if not is_candidate(item):
        return None
    minute, goals_home, goals_away = _score_and_minute(item)  # type: ignore[misc]
    teams = item.get("teams") or {}
    ids = {side: to_int((teams.get(side) or {}).get("id")) for side in ("home", "away")}
    league = item.get("league") or {}
    for side, other in (("home", "away"), ("away", "home")):
        if ids[side] is None:
            continue
        stats = _team_stats(item, ids[side])
        possession = to_percent(stats.get("Ball Possession"))
        shots = to_int(stats.get("Shots on Goal"))
        if possession is None or shots is None:
            continue
        if possession <= POSSESSION_ABOVE or shots < SHOTS_ON_GOAL_MIN:
            continue
        return Alert(
            fixture_id=int(item["fixture"]["id"]),
            league_id=to_int(league.get("id")),
            league=str(league.get("name") or ""),
            country=str(league.get("country") or ""),
            home=str((teams.get("home") or {}).get("name") or ""),
            away=str((teams.get("away") or {}).get("name") or ""),
            minute=minute,
            goals_home=goals_home,
            goals_away=goals_away,
            team_side=side,
            possession=possession,
            shots=shots,
            opponent_shots=to_int(_team_stats(item, ids[other]).get("Shots on Goal")),
            kickoff_at=to_utc((item.get("fixture") or {}).get("date")),
        )
    return None


def format_message(alert: Alert) -> str:
    header = f"ID: {alert.alert_id}\n" if alert.alert_id is not None else "ID: (dry-run)\n"
    role = "хозяева" if alert.team_side == "home" else "гости"
    lines = [
        f"{header}Тип: {CATEGORY_TITLES[alert.category]}",
        f"Лига: {alert.league} ({alert.country})",
        f"Матч: {alert.home} — {alert.away}",
        f"Минута: {alert.minute}' | Счёт: {alert.goals_home}:{alert.goals_away}",
        f"Команда: {alert.team} ({role})",
    ]
    if alert.category == "favorite":
        lines[4] = f"Фаворит: {alert.home} — {alert.home_rank}-е место из {alert.teams_count}, {alert.home_points} оч."
        lines.append(f"Аутсайдер: {alert.away} — {alert.away_rank}-е место, {alert.away_points} оч.")
    elif alert.category == "half":
        total = f" (за матч {alert.total_possession} %)" if alert.total_possession is not None else ""
        lines.append(f"Владение во 2-м тайме: {alert.possession} %{total}")
    elif alert.category == "half_attack":
        opponent = "?" if alert.opponent_shots is None else alert.opponent_shots
        lines.append(f"Во 2-м тайме: владение {alert.possession} %, удары в створ {alert.shots} (у соперника {opponent})")
    else:
        opponent = "?" if alert.opponent_shots is None else alert.opponent_shots
        lines.append(f"Владение: {alert.possession} %")
        lines.append(f"Удары в створ: {alert.shots} против {opponent}")
    return "\n".join(lines)


# ------------------------------------------------------------------ Telegram


def _curl_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def telegram_curl_request(token: str, chat_id: str, text: str, proxy: str | None, timeout: int = 15):
    """Аргументы curl и конфиг для stdin. Токен только в конфиге: в argv его видно в `ps`."""
    argv = ["curl", "-sS", "-m", str(timeout), "-o", "/dev/null", "-w", "%{http_code}", "-K", "-"]
    if proxy:
        argv += ["--proxy", proxy]
    config = (
        f'url = "https://api.telegram.org/bot{_curl_quote(token)}/sendMessage"\n'
        f'data-urlencode = "chat_id={_curl_quote(chat_id)}"\n'
        f'data-urlencode = "text={_curl_quote(text)}"\n'
    )
    return argv, config


def telegram_sender_from_env() -> Sender:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    proxy = os.environ.get("TELEGRAM_PROXY") or None
    if not token or not chat_id:
        raise SystemExit("Не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")

    def send(text: str) -> None:
        argv, config = telegram_curl_request(token, chat_id, text, proxy)
        try:
            result = subprocess.run(argv, input=config, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise TelegramError("таймаут отправки") from None
        if result.returncode != 0:
            raise TelegramError(f"curl завершился с кодом {result.returncode}")
        code = result.stdout.strip()
        if code != "200":
            raise TelegramError(f"Telegram ответил HTTP {code}")

    return send


# ------------------------------------------------------------------- запуск


# ------------------------------------------------- категория half: владение во 2-м тайме


def _side_values(item: dict, stat_type: str, convert) -> dict[str, int]:
    """Значение показателя за матч по сторонам: {'home': 58, 'away': 42}; пропущенные не включаются."""
    teams = item.get("teams") or {}
    result = {}
    for side in ("home", "away"):
        team_id = to_int((teams.get(side) or {}).get("id"))
        value = convert(_team_stats(item, team_id).get(stat_type)) if team_id is not None else None
        if value is not None:
            result[side] = value
    return result


def _possession_by_side(item: dict) -> dict[str, int]:
    return _side_values(item, "Ball Possession", to_percent)


def _shots_by_side(item: dict) -> dict[str, int]:
    return _side_values(item, "Shots on Goal", to_int)


def estimate_second_half(total: int, first_half: int, minute: int) -> float | None:
    """Оценка владения во втором тайме по итогу за матч и владению в первом.

    Предполагает, что доля владения распределена по времени равномерно (первый тайм
    считается равным 45 минутам); поэтому оценка приближённая и служит только отбору
    кандидатов для точного запроса по таймам.
    """
    if minute <= FIRST_HALF_MINUTES:
        return None
    return (total * minute - first_half * FIRST_HALF_MINUTES) / (minute - FIRST_HALF_MINUTES)


def _half_stats_exact(body: dict | None) -> dict[int, dict[str, int | None]]:
    """team_id -> {'possession', 'shots'} за второй тайм из ответа `fixtures/statistics?half=true`."""
    result = {}
    for entry in (body or {}).get("response") or []:
        team_id = to_int((entry.get("team") or {}).get("id"))
        rows = {r.get("type"): r.get("value") for r in entry.get("statistics_2h") or []}
        if team_id is not None:
            result[team_id] = {
                "possession": to_percent(rows.get("Ball Possession")),
                "shots": to_int(rows.get("Shots on Goal")),
            }
    return result


def _base_alert(item: dict, side: str, category: str, **fields) -> Alert:
    minute, goals_home, goals_away = _score_and_minute(item)  # type: ignore[misc]
    teams = item.get("teams") or {}
    league = item.get("league") or {}
    return Alert(
        fixture_id=int(item["fixture"]["id"]),
        league_id=to_int(league.get("id")),
        league=str(league.get("name") or ""),
        country=str(league.get("country") or ""),
        home=str((teams.get("home") or {}).get("name") or ""),
        away=str((teams.get("away") or {}).get("name") or ""),
        minute=minute,
        goals_home=goals_home,
        goals_away=goals_away,
        team_side=side,
        kickoff_at=to_utc((item.get("fixture") or {}).get("date")),
        category=category,
        **fields,
    )


def _lead(item: dict, side: str) -> int:
    _, home, away = _score_and_minute(item)  # type: ignore[misc]
    return home - away if side == "home" else away - home


def is_half_candidate(item: dict) -> bool:
    """Идущий тайм и минута 65–75."""
    parsed = _score_and_minute(item)
    return parsed is not None and HALF_MINUTE_FROM <= parsed[0] <= MINUTE_TO


def build_half_alert(item: dict, exact: dict[int, dict], total: dict[str, int]) -> Alert | None:
    """Категория half: минута 65–75, владение команды во втором тайме не меньше 70 %,
    команда проигрывает, играет вничью или ведёт не больше чем на один мяч."""
    if not is_half_candidate(item):
        return None
    teams = item.get("teams") or {}
    for side in ("home", "away"):
        team_id = to_int((teams.get(side) or {}).get("id"))
        value = (exact.get(team_id) or {}).get("possession") if team_id is not None else None
        if value is None or value < HALF_POSSESSION_MIN or _lead(item, side) > HALF_MAX_LEAD:
            continue
        return _base_alert(item, side, "half", possession=value, shots=None, opponent_shots=None,
                           total_possession=total.get(side))
    return None


def build_attack_alert(item: dict, exact: dict[int, dict], total: dict[str, int]) -> Alert | None:
    """Категория half_attack: во втором тайме владение больше 55 % и 4 и более ударов в створ."""
    if not is_candidate(item):
        return None
    teams = item.get("teams") or {}
    for side, other in (("home", "away"), ("away", "home")):
        team_id = to_int((teams.get(side) or {}).get("id"))
        other_id = to_int((teams.get(other) or {}).get("id"))
        stats = exact.get(team_id) or {}
        possession, shots = stats.get("possession"), stats.get("shots")
        if possession is None or shots is None:
            continue
        if possession <= ATTACK_POSSESSION_ABOVE or shots < ATTACK_SHOTS_MIN:
            continue
        return _base_alert(item, side, "half_attack", possession=possession, shots=shots,
                           opponent_shots=(exact.get(other_id) or {}).get("shots"),
                           total_possession=total.get(side))
    return None


# ------------------------------------------------------------------ категория favorite


def favorite_places(teams_count: int) -> int:
    """Число мест в верхней и в нижней группе: 20 % команд, половина округляется вверх."""
    return max(1, int(FAVORITE_SHARE * teams_count + 0.5))


def load_table(conn, league_id: int, season: int) -> dict[int, dict[str, int]]:
    """Таблица лиги по сыгранным матчам (статус FT): team_id -> {rank, points, played}."""
    with conn.cursor() as cur:
        cur.execute(
            """WITH sides AS (
                   SELECT home_team_id AS team_id, goals_home AS gf, goals_away AS ga FROM fixtures
                    WHERE league_id = %(l)s AND season = %(s)s AND status_short = 'FT' AND goals_home IS NOT NULL
                   UNION ALL
                   SELECT away_team_id, goals_away, goals_home FROM fixtures
                    WHERE league_id = %(l)s AND season = %(s)s AND status_short = 'FT' AND goals_home IS NOT NULL)
               SELECT team_id,
                      sum(CASE WHEN gf > ga THEN 3 WHEN gf = ga THEN 1 ELSE 0 END)::int AS points,
                      sum(gf - ga)::int AS goal_diff, sum(gf)::int AS goals_for, count(*)::int AS played
                 FROM sides GROUP BY team_id""",
            {"l": league_id, "s": season},
        )
        rows = cur.fetchall()
    rows.sort(key=lambda r: (-r[1], -r[2], -r[3], r[0]))
    return {r[0]: {"rank": i + 1, "points": r[1], "played": r[4]} for i, r in enumerate(rows)}


def is_favorite_candidate(item: dict) -> bool:
    """Ф-1, Ф-2: идущий тайм, минута 65–75, хозяева не выигрывают."""
    parsed = _score_and_minute(item)
    if parsed is None:
        return False
    minute, home, away = parsed
    return FAVORITE_MINUTE_FROM <= minute <= FAVORITE_MINUTE_TO and home <= away


def build_favorite_alert(item: dict, table: dict[int, dict[str, int]], league_type: str | None) -> Alert | None:
    """Ф-3, Ф-4 и защиты таблицы. Таблица строится по базе (`load_table`)."""
    if league_type != "League" or not is_favorite_candidate(item):
        return None
    teams = item.get("teams") or {}
    home = table.get(to_int((teams.get("home") or {}).get("id")))
    away = table.get(to_int((teams.get("away") or {}).get("id")))
    count = len(table)
    if home is None or away is None or count < MIN_TEAMS:
        return None
    if home["played"] < MIN_PLAYED or away["played"] < MIN_PLAYED:
        return None
    places = favorite_places(count)
    if home["rank"] > places or away["rank"] <= count - places:
        return None
    return _base_alert(item, "home", "favorite", possession=None, shots=None, opponent_shots=None,
                       home_rank=home["rank"], away_rank=away["rank"], teams_count=count,
                       home_points=home["points"], away_points=away["points"])


def _league_types(conn, league_ids: list[int]) -> dict[int, str | None]:
    if not league_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT league_id, type FROM leagues WHERE league_id = ANY(%s)", (league_ids,))
        return dict(cur.fetchall())


# ------------------------------------------------------------------ работа с базой


def _already_sent(conn, fixture_ids: list[int], category: str) -> set[int]:
    if not fixture_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fixture_id FROM live_alerts_sent WHERE fixture_id = ANY(%s) AND category = %s",
            (fixture_ids, category),
        )
        return {row[0] for row in cur.fetchall()}


def _reserve_alert_id(conn) -> int:
    """Номер оповещения резервируется до отправки, чтобы попасть в текст сообщения."""
    with conn.cursor() as cur:
        cur.execute("SELECT nextval(pg_get_serial_sequence('live_alerts_sent', 'alert_id'))")
        return cur.fetchone()[0]


def _record_sent(conn, alert: Alert) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO live_alerts_sent (alert_id, fixture_id, category, league_id, home_name, away_name,
                   minute, goals_home, goals_away, team_side, possession, shots_on_goal, opponent_shots_on_goal,
                   total_possession, kickoff_at, home_rank, away_rank, teams_count)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (fixture_id, category) DO NOTHING""",
            (alert.alert_id, alert.fixture_id, alert.category, alert.league_id, alert.home, alert.away,
             alert.minute, alert.goals_home, alert.goals_away, alert.team_side, alert.possession, alert.shots,
             alert.opponent_shots, alert.total_possession, alert.kickoff_at,
             alert.home_rank, alert.away_rank, alert.teams_count),
        )


def _ht_snapshots(conn, fixture_ids: list[int]) -> dict[int, dict[str, dict[str, int | None]]]:
    """fixture_id -> {'possession': {'home','away'}, 'shots': {'home','away'}} на перерыве."""
    if not fixture_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT fixture_id, home_possession, away_possession, home_shots_on_goal, away_shots_on_goal
               FROM live_ht_possession WHERE fixture_id = ANY(%s)""",
            (fixture_ids,),
        )
        return {
            row[0]: {"possession": {"home": row[1], "away": row[2]}, "shots": {"home": row[3], "away": row[4]}}
            for row in cur.fetchall()
        }


def _save_ht_snapshot(conn, fixture_id: int, possession: dict[str, int], shots: dict[str, int]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO live_ht_possession (fixture_id, home_possession, away_possession,
                   home_shots_on_goal, away_shots_on_goal)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (fixture_id) DO NOTHING""",
            (fixture_id, possession["home"], possession["away"], shots.get("home"), shots.get("away")),
        )


def _purge_old_snapshots(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM live_ht_possession WHERE captured_at < now() - interval '2 days'")


def _budget_left(client: ApiClient) -> bool:
    return not client.quota_exhausted and client.requests_used < client.max_requests


def _fetch_by_ids(client: ApiClient, ids: list[int]):
    """Матчи пачками по 20: итератор по элементам ответа `fixtures?ids=`. Останавливается по бюджету."""
    for start in range(0, len(ids), IDS_BATCH):
        if not _budget_left(client):
            return
        batch = ids[start:start + IDS_BATCH]
        body = client.get("/fixtures", {"ids": "-".join(map(str, batch))}, use_cache=False)
        if body is None or body.get("errors"):
            if not _budget_left(client):
                return
            raise RuntimeError("не удалось получить статистику матчей")
        yield from body.get("response") or []


def run_live_alerts(
    client: ApiClient, conn, send: Sender | None, dry_run: bool = False, log: Callable[[str], None] = print,
    categories: tuple[str, ...] | None = None,
):
    """Один запуск. `send` не нужен при dry_run (сообщение пишется в журнал)."""
    categories = ENABLED_CATEGORIES if categories is None else categories
    legacy = any(c in categories for c in LEGACY_CATEGORIES)
    failed_sends = 0
    with collection_run(conn, client, "live_alerts") as ctx:

        def deliver(alert: Alert) -> bool:
            nonlocal failed_sends
            if dry_run:
                log(f"[dry-run] сработал матч {alert.fixture_id} ({alert.category}):\n{format_message(alert)}")
                ctx.items_processed += 1
                return True
            alert = replace(alert, alert_id=_reserve_alert_id(conn))
            try:
                send(format_message(alert))  # type: ignore[misc]
            except TelegramError as exc:
                log(f"оповещение по матчу {alert.fixture_id} не отправлено: {exc}")
                failed_sends += 1
                return False
            _record_sent(conn, alert)
            ctx.items_processed += 1
            return True

        live = client.get("/fixtures", {"live": "all"}, use_cache=False)
        if live is None or live.get("errors"):
            if _budget_left(client) and not client.quota_exhausted:
                raise RuntimeError("не удалось получить идущие матчи")
            return ctx
        items = live.get("response") or []

        if "favorite" in categories:
            fav = [i for i in items if is_favorite_candidate(i)]
            fav_ids = [int(i["fixture"]["id"]) for i in fav]
            fav_sent = _already_sent(conn, fav_ids, "favorite")
            fav = [i for i in fav if int(i["fixture"]["id"]) not in fav_sent]
            types = _league_types(conn, sorted({to_int((i.get("league") or {}).get("id")) for i in fav} - {None}))
            tables: dict[tuple[int, int], dict] = {}
            for item in fav:
                league = item.get("league") or {}
                league_id, season = to_int(league.get("id")), to_int(league.get("season"))
                if league_id is None or season is None:
                    continue
                if (league_id, season) not in tables:
                    tables[(league_id, season)] = load_table(conn, league_id, season)
                alert = build_favorite_alert(item, tables[(league_id, season)], types.get(league_id))
                if alert is not None:
                    deliver(alert)

        if legacy:
            # Владение на перерыве: итог на перерыве равен первому тайму.
            ht_ids = [int(i["fixture"]["id"]) for i in items if ((i.get("fixture") or {}).get("status") or {}).get("short") == "HT"]
            known = _ht_snapshots(conn, ht_ids)
            for item in _fetch_by_ids(client, [i for i in ht_ids if i not in known]):
                possession = _possession_by_side(item)
                if len(possession) == 2:
                    _save_ht_snapshot(conn, int(item["fixture"]["id"]), possession, _shots_by_side(item))
            _purge_old_snapshots(conn)

            candidates = [item for item in items if is_candidate(item)]
            ids = [int(item["fixture"]["id"]) for item in candidates]
            sent = {c: _already_sent(conn, ids, c) for c in CATEGORY_TITLES}
            snapshots = _ht_snapshots(conn, ids)

            on = {c: c in categories for c in LEGACY_CATEGORIES}

            def second_half_pending(fixture_id: int) -> bool:
                return fixture_id in snapshots and (
                    (on["half"] and fixture_id not in sent["half"])
                    or (on["half_attack"] and fixture_id not in sent["half_attack"]))

            need = [i for i in ids if (on["stats"] and i not in sent["stats"]) or second_half_pending(i)]
            for item in _fetch_by_ids(client, need):
                fixture_id = int(item["fixture"]["id"])
                fired = False
                if on["stats"] and fixture_id not in sent["stats"]:
                    alert = build_alert(item)
                    if alert is not None:
                        fired = deliver(alert) or fired
                if second_half_pending(fixture_id) and is_candidate(item):
                    minute = _score_and_minute(item)[0]  # type: ignore[index]
                    total = _possession_by_side(item)
                    total_shots = _shots_by_side(item)
                    snap = snapshots[fixture_id]
                    want_half = want_attack = False
                    for side in ("home", "away"):
                        first = snap["possession"].get(side)
                        est = estimate_second_half(total[side], first, minute) if side in total and first is not None else None
                        if (on["half"] and fixture_id not in sent["half"] and is_half_candidate(item)
                                and _lead(item, side) <= HALF_MAX_LEAD and est is not None and est > HALF_PREFILTER):
                            want_half = True
                        shots_first = snap["shots"].get(side)
                        if (on["half_attack"] and fixture_id not in sent["half_attack"] and side in total_shots and shots_first is not None
                                and total_shots[side] - shots_first >= ATTACK_SHOTS_MIN
                                and (est is None or est > ATTACK_PREFILTER_POSSESSION)):
                            want_attack = True
                    if (want_half or want_attack) and _budget_left(client):
                        body = client.get("/fixtures/statistics", {"fixture": fixture_id, "half": "true"}, use_cache=False)
                        exact = _half_stats_exact(body)
                        if want_half:
                            alert = build_half_alert(item, exact, total)
                            if alert is not None:
                                fired = deliver(alert) or fired
                        if want_attack:
                            alert = build_attack_alert(item, exact, total)
                            if alert is not None:
                                fired = deliver(alert) or fired
                if not fired:
                    ctx.skipped += 1
        if failed_sends:
            raise RuntimeError(f"не отправлено оповещений: {failed_sends}")
    return ctx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Оповещения по идущим матчам")
    parser.add_argument("--dry-run", action="store_true", help="не отправлять, печатать в журнал")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--cache-dir", type=Path, default=Path("cache"))
    args = parser.parse_args(argv)

    url = database_url()  # заодно подхватывает .env
    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        print("Не задана переменная окружения APIFOOTBALL_KEY", file=sys.stderr)
        return 1
    send = None if args.dry_run else telegram_sender_from_env()
    client = ApiClient(key=key, cache_dir=args.cache_dir, max_requests=args.max_requests, verbose=False)
    try:
        with connect(url) as conn:
            ctx = run_live_alerts(client, conn, send, dry_run=args.dry_run)
    except Exception as exc:
        print(f"ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"срабатываний: {ctx.items_processed}, пропущено: {ctx.skipped}, запросов: {client.requests_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
