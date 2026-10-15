"""Оповещения по идущим матчам (specs/онлайн-оповещения.md).

Каждый запуск: один запрос `fixtures?live=all`, отбор кандидатов по минуте и
счёту, затем статистика кандидатов пачками `fixtures?ids=…` (до 20 матчей).
Подходящий матч уходит в Telegram один раз. Снимки матчей не сохраняются.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.api.client import ApiClient
from src.common.convert import to_int, to_percent
from src.db.connection import connect, database_url
from src.jobs.collect import collection_run

LIVE_STATUSES = ("1H", "2H")
MINUTE_FROM, MINUTE_TO = 60, 75
POSSESSION_ABOVE = 55
SHOTS_ON_GOAL_MIN = 6
IDS_BATCH = 20
DEFAULT_MAX_REQUESTS = 10

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
    possession_home: int
    shots_home: int
    shots_away: int | None


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
    """Т-1 и Т-2: минута 60–75 и хозяева не выигрывают."""
    parsed = _score_and_minute(item)
    if parsed is None:
        return False
    minute, home, away = parsed
    return MINUTE_FROM <= minute <= MINUTE_TO and home <= away


def _team_stats(item: dict, team_id: int | None) -> dict[str, object]:
    for entry in item.get("statistics") or []:
        if to_int((entry.get("team") or {}).get("id")) == team_id:
            return {row.get("type"): row.get("value") for row in entry.get("statistics") or []}
    return {}


def build_alert(item: dict) -> Alert | None:
    """Полный триггер Т-1…Т-4 по ответу `ids=`. None — матч не подходит или данных нет."""
    if not is_candidate(item):
        return None
    minute, goals_home, goals_away = _score_and_minute(item)  # type: ignore[misc]
    teams = item.get("teams") or {}
    home_id = to_int((teams.get("home") or {}).get("id"))
    away_id = to_int((teams.get("away") or {}).get("id"))
    if home_id is None:
        return None
    home_stats = _team_stats(item, home_id)
    possession = to_percent(home_stats.get("Ball Possession"))
    shots = to_int(home_stats.get("Shots on Goal"))
    if possession is None or shots is None:
        return None
    if possession <= POSSESSION_ABOVE or shots < SHOTS_ON_GOAL_MIN:
        return None
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
        possession_home=possession,
        shots_home=shots,
        shots_away=to_int(_team_stats(item, away_id).get("Shots on Goal")),
    )


def format_message(alert: Alert) -> str:
    away_shots = "?" if alert.shots_away is None else alert.shots_away
    return (
        f"Лига: {alert.league} ({alert.country})\n"
        f"Матч: {alert.home} — {alert.away}\n"
        f"Минута: {alert.minute}' | Счёт: {alert.goals_home}:{alert.goals_away}\n"
        f"Владение хозяев: {alert.possession_home} %\n"
        f"Удары в створ: хозяева {alert.shots_home}, гости {away_shots}"
    )


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


def _already_sent(conn, fixture_ids: list[int]) -> set[int]:
    if not fixture_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT fixture_id FROM live_alerts_sent WHERE fixture_id = ANY(%s)", (fixture_ids,))
        return {row[0] for row in cur.fetchall()}


def _record_sent(conn, alert: Alert) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO live_alerts_sent (fixture_id, league_id, home_name, away_name, minute,
                   goals_home, goals_away, possession_home, shots_on_goal_home, shots_on_goal_away)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (fixture_id) DO NOTHING""",
            (alert.fixture_id, alert.league_id, alert.home, alert.away, alert.minute,
             alert.goals_home, alert.goals_away, alert.possession_home, alert.shots_home, alert.shots_away),
        )


def _budget_left(client: ApiClient) -> bool:
    return not client.quota_exhausted and client.requests_used < client.max_requests


def run_live_alerts(
    client: ApiClient, conn, send: Sender | None, dry_run: bool = False, log: Callable[[str], None] = print
):
    """Один запуск. `send` не нужен при dry_run (сообщение пишется в журнал)."""
    failed_sends = 0
    with collection_run(conn, client, "live_alerts") as ctx:
        live = client.get("/fixtures", {"live": "all"}, use_cache=False)
        if live is None or live.get("errors"):
            if _budget_left(client) and not client.quota_exhausted:
                raise RuntimeError("не удалось получить идущие матчи")
            return ctx
        candidates = [item for item in live.get("response") or [] if is_candidate(item)]
        ids = [int(item["fixture"]["id"]) for item in candidates]
        sent = _already_sent(conn, ids)
        fresh_ids = [i for i in ids if i not in sent]
        for start in range(0, len(fresh_ids), IDS_BATCH):
            if not _budget_left(client):
                break
            batch = fresh_ids[start:start + IDS_BATCH]
            body = client.get("/fixtures", {"ids": "-".join(map(str, batch))}, use_cache=False)
            if body is None or body.get("errors"):
                if not _budget_left(client):
                    break
                raise RuntimeError("не удалось получить статистику матчей")
            for item in body.get("response") or []:
                alert = build_alert(item)
                if alert is None:
                    ctx.skipped += 1
                    continue
                message = format_message(alert)
                if dry_run:
                    log(f"[dry-run] сработал матч {alert.fixture_id}:\n{message}")
                    ctx.items_processed += 1
                    continue
                try:
                    send(message)  # type: ignore[misc]
                except TelegramError as exc:
                    log(f"оповещение по матчу {alert.fixture_id} не отправлено: {exc}")
                    failed_sends += 1
                    continue
                _record_sent(conn, alert)
                ctx.items_processed += 1
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
