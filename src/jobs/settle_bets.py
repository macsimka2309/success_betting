"""Расчёт результатов ставок после матчей (specs/реестр-ставок.md, РС-1…РС-6).

Берёт открытые ставки, одним запросом на 20 матчей узнаёт статус и счёт основного
времени и проставляет результат. Повторный запуск уже рассчитанные ставки не трогает.
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


def _fixture_results(client: ApiClient, fixture_ids: list[int]) -> dict[int, tuple[str, int | None, int | None]]:
    """fixture_id -> (статус, голы хозяев, голы гостей) по основному времени."""
    results: dict[int, tuple[str, int | None, int | None]] = {}
    for start in range(0, len(fixture_ids), IDS_BATCH):
        if client.quota_exhausted or client.requests_used >= client.max_requests:
            break
        batch = fixture_ids[start:start + IDS_BATCH]
        body = client.get("/fixtures", {"ids": "-".join(map(str, batch))}, use_cache=False)
        if body is None or body.get("errors"):
            continue
        for item in body.get("response") or []:
            fixture_id = to_int((item.get("fixture") or {}).get("id"))
            status = ((item.get("fixture") or {}).get("status") or {}).get("short")
            fulltime = (item.get("score") or {}).get("fulltime") or {}
            if fixture_id is not None:
                results[fixture_id] = (status, to_int(fulltime.get("home")), to_int(fulltime.get("away")))
    return results


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
        if not open_bets:
            return ctx
        results = _fixture_results(client, sorted({row[1] for row in open_bets}))
        for bet_id, fixture_id, market, line, stake, odds, started, stuck_notified in open_bets:
            status, goals_home, goals_away = results.get(fixture_id, (None, None, None))
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
    print(f"рассчитано ставок: {ctx.items_processed}, предупреждений: {ctx.skipped}, запросов: {client.requests_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
