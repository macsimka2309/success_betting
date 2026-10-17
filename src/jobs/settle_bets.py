"""Расчёт результатов ставок и проходимости оповещений после матчей.

Ставки (specs/реестр-ставок.md, РС-1…РС-6): берёт открытые ставки, одним запросом на 20
матчей узнаёт статус и счёт основного времени и проставляет результат.
Оповещения (specs/онлайн-оповещения.md, «Учёт проходимости»): в том же ответе есть события
матча, по ним определяется, был ли гол после 75-й минуты. Повторный запуск уже обработанное
не трогает.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from src.api.client import ApiClient
from src.bets.settle import profit, settle
from src.common.convert import to_int
from src.db.connection import connect, database_url
from src.jobs.collect import collection_run

FINISHED = ("FT", "AET", "PEN")
VOID_STATUSES = ("PST", "CANC", "ABD", "AWD", "WO")
IDS_BATCH = 20
STUCK_AFTER = timedelta(hours=48)
DEFAULT_MAX_REQUESTS = 5
GOAL_AFTER_MINUTE = 75
REGULATION_END = 90
ALERT_MIN_AGE = "40 minutes"


def _fetch_fixtures(client: ApiClient, fixture_ids: list[int]) -> dict[int, dict]:
    """fixture_id -> элемент ответа `fixtures?ids=` (статус, счёт, события)."""
    items: dict[int, dict] = {}
    for start in range(0, len(fixture_ids), IDS_BATCH):
        if client.quota_exhausted or client.requests_used >= client.max_requests:
            break
        batch = fixture_ids[start:start + IDS_BATCH]
        body = client.get("/fixtures", {"ids": "-".join(map(str, batch))}, use_cache=False)
        if body is None or body.get("errors"):
            continue
        for item in body.get("response") or []:
            fixture_id = to_int((item.get("fixture") or {}).get("id"))
            if fixture_id is not None:
                items[fixture_id] = item
    return items


def _status_and_score(item: dict | None) -> tuple[str | None, int | None, int | None]:
    if item is None:
        return None, None, None
    status = ((item.get("fixture") or {}).get("status") or {}).get("short")
    fulltime = (item.get("score") or {}).get("fulltime") or {}
    return status, to_int(fulltime.get("home")), to_int(fulltime.get("away"))


def count_regulation_goals(events: list[dict]) -> tuple[int, int] | None:
    """(голов в основное время, из них после 75-й минуты) по событиям матча.

    Незабитые пенальти не считаются; дополнительное время (минута больше 90) и серия
    пенальти не входят. None — у гола нет минуты, оценить нельзя.
    """
    regulation = after = 0
    for event in events or []:
        if event.get("type") != "Goal" or event.get("detail") == "Missed Penalty":
            continue
        minute = to_int((event.get("time") or {}).get("elapsed"))
        if minute is None:
            return None
        if minute > REGULATION_END:
            continue
        regulation += 1
        if minute > GOAL_AFTER_MINUTE:
            after += 1
    return regulation, after


def classify_alert(item: dict | None) -> tuple[str, int | None, int | None, int | None] | None:
    """(outcome, голов после 75-й, счёт хозяев, счёт гостей) или None, если матч не закончен."""
    status, home, away = _status_and_score(item)
    if status in VOID_STATUSES:
        return "void", None, None, None
    if status not in FINISHED:
        return None
    if home is None or away is None:
        return "no_data", None, None, None
    counted = count_regulation_goals((item or {}).get("events") or [])
    if counted is None or counted[0] != home + away:
        return "no_data", None, home, away
    return ("hit" if counted[1] >= 1 else "miss"), counted[1], home, away


def _resolve_alerts(conn, items: dict[int, dict], pending: list[tuple[int, int]]) -> int:
    resolved = 0
    for alert_id, fixture_id in pending:
        classified = classify_alert(items.get(fixture_id))
        if classified is None:
            continue
        outcome, after, home, away = classified
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE live_alerts_sent SET outcome = %s, goals_after_75 = %s, final_goals_home = %s,
                          final_goals_away = %s, resolved_at = now()
                   WHERE alert_id = %s AND outcome = 'pending'""",
                (outcome, after, home, away, alert_id),
            )
        resolved += 1
    return resolved


def run_settle(
    client: ApiClient, conn, notify: Callable[[str], None] | None = None, now: datetime | None = None
):
    now = now or datetime.now(timezone.utc)
    with collection_run(conn, client, "settle_bets") as ctx:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT bet_id, fixture_id, market, line, stake, odds,
                          COALESCE(kickoff_at, created_at), stuck_notified_at
                   FROM bets WHERE status = 'open' ORDER BY bet_id"""
            )
            open_bets = cur.fetchall()
            cur.execute(
                f"""SELECT alert_id, fixture_id FROM live_alerts_sent
                    WHERE outcome = 'pending' AND sent_at < now() - interval '{ALERT_MIN_AGE}'
                    ORDER BY alert_id"""
            )
            pending_alerts = cur.fetchall()
        if not open_bets and not pending_alerts:
            return ctx
        fixture_ids = sorted({row[1] for row in open_bets} | {row[1] for row in pending_alerts})
        items = _fetch_fixtures(client, fixture_ids)
        ctx.items_processed += _resolve_alerts(conn, items, pending_alerts)
        for bet_id, fixture_id, market, line, stake, odds, started, stuck_notified in open_bets:
            status, goals_home, goals_away = _status_and_score(items.get(fixture_id))
            outcome = None
            if status in FINISHED and goals_home is not None and goals_away is not None:
                outcome = settle(market, Decimal(line), goals_home, goals_away)
            elif status in VOID_STATUSES:
                outcome = "void"
            if outcome is not None:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE bets SET status = %s, goals_home = %s, goals_away = %s,
                                  profit = %s, settled_at = now()
                           WHERE bet_id = %s AND status = 'open'""",
                        (outcome, goals_home, goals_away, profit(outcome, Decimal(stake), Decimal(odds)), bet_id),
                    )
                ctx.items_processed += 1
            elif notify and stuck_notified is None and now - started > STUCK_AFTER:
                try:
                    notify(f"Не удалось рассчитать ставку №{bet_id}: нет итогового счёта матча {fixture_id}")
                except Exception:
                    continue  # не отправилось: пометки нет, повторим при следующем запуске
                with conn.cursor() as cur:
                    cur.execute("UPDATE bets SET stuck_notified_at = now() WHERE bet_id = %s", (bet_id,))
                ctx.skipped += 1
    return ctx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Расчёт результатов ставок")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--cache-dir", type=Path, default=Path("cache"))
    args = parser.parse_args(argv)

    url = database_url()
    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        print("Не задана переменная окружения APIFOOTBALL_KEY", file=sys.stderr)
        return 1
    notify = None
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from src.jobs.live_alerts import telegram_sender_from_env

        notify = telegram_sender_from_env()
    client = ApiClient(key=key, cache_dir=args.cache_dir, max_requests=args.max_requests, verbose=False)
    try:
        with connect(url) as conn:
            ctx = run_settle(client, conn, notify)
    except Exception as exc:
        print(f"ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"рассчитано ставок и оповещений: {ctx.items_processed}, предупреждений: {ctx.skipped}, запросов: {client.requests_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
