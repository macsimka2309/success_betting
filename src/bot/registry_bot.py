"""Бот реестра ставок (specs/реестр-ставок.md): внести событие и показать результат.

Работает постоянным процессом: забирает обновления Telegram (long polling) и
отвечает только владельцу. Состояние диалога хранится в базе (`bot_dialogs`).
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from src.bot.telegram_api import ApiError, TelegramApi
from src.db.connection import connect, database_url

MENU_ADD = "Внести событие"
MENU_REPORT = "Показать результат"
CANCEL = "Отмена"
MARKET_TITLES = {"TB": "ТБ", "ITB1": "ИТБ1", "ITB2": "ИТБ2"}
MENU = {"keyboard": [[{"text": MENU_ADD}, {"text": MENU_REPORT}]], "resize_keyboard": True}
MARKET_KEYBOARD = {
    "inline_keyboard": [
        [{"text": title, "callback_data": f"mkt:{code}"} for code, title in MARKET_TITLES.items()],
        [{"text": CANCEL, "callback_data": "cancel"}],
    ]
}
REPORT_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "По дням", "callback_data": "rep:day"},
        {"text": "По месяцам", "callback_data": "rep:month"},
    ]]
}
NUMBER = re.compile(r"^[0-9]+(\.[0-9]+)?$")
INTEGER = re.compile(r"^[0-9]{1,9}$")
QUARTER = Decimal("0.25")


def parse_number(text: str) -> Decimal | None:
    """Число с точкой или запятой; всё остальное (экспонента, nan, минус) отвергается."""
    cleaned = text.strip().replace(",", ".")
    if not NUMBER.match(cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


# ------------------------------------------------------------------- отчёт

_REPORT_SQL = {
    "day": """
        SELECT to_char(m::date, 'YYYY-MM-DD'), count(*), COALESCE(sum(profit), 0), count(*) FILTER (WHERE status = 'open')
        FROM (SELECT COALESCE(kickoff_at, created_at) AT TIME ZONE 'Europe/Moscow' AS m, profit, status FROM bets) b
        WHERE m::date >= (now() AT TIME ZONE 'Europe/Moscow')::date - 30
        GROUP BY 1 ORDER BY 1 DESC""",
    "month": """
        SELECT to_char(m, 'YYYY-MM'), count(*), COALESCE(sum(profit), 0), count(*) FILTER (WHERE status = 'open')
        FROM (SELECT COALESCE(kickoff_at, created_at) AT TIME ZONE 'Europe/Moscow' AS m, profit, status FROM bets) b
        WHERE m >= date_trunc('month', (now() AT TIME ZONE 'Europe/Moscow') - interval '11 months')
        GROUP BY 1 ORDER BY 1 DESC""",
}


def report_rows(conn, mode: str) -> list[tuple[str, int, Decimal, int]]:
    with conn.cursor() as cur:
        cur.execute(_REPORT_SQL[mode])
        return cur.fetchall()


def format_report(rows: list[tuple[str, int, Decimal, int]], mode: str) -> str:
    title = "День" if mode == "day" else "Месяц"
    if not rows:
        return "Ставок за период нет"
    lines = [f"{title:<11}{'Ставок':>8}{'Результат':>12}"]
    for period, count, result, _open in rows:
        lines.append(f"{period:<11}{count:>8}{result:>+12.2f}")
    total_count = sum(r[1] for r in rows)
    total = sum((r[2] for r in rows), Decimal(0))
    lines.append(f"{'Итого':<11}{total_count:>8}{total:>+12.2f}")
    text = "<pre>" + html.escape("\n".join(lines)) + "</pre>"
    pending = sum(r[3] for r in rows)
    if pending:
        text += f"\nНе рассчитано: {pending}"
    return text


# --------------------------------------------------------------------- бот


class Bot:
    def __init__(self, api: TelegramApi, conn, owner_chat_id: int, log=print) -> None:
        self.api = api
        self.conn = conn
        self.owner = owner_chat_id
        self.log = log

    # ---- состояние диалога
    def _dialog(self) -> tuple[str | None, dict, int | None]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT step, payload, last_update_id FROM bot_dialogs WHERE chat_id = %s", (self.owner,))
            row = cur.fetchone()
        return (row[0], row[1], row[2]) if row else (None, {}, None)

    def _set_dialog(self, step: str | None, payload: dict, update_id: int | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO bot_dialogs (chat_id, step, payload, last_update_id)
                   VALUES (%s, %s, %s::jsonb, %s)
                   ON CONFLICT (chat_id) DO UPDATE SET step = EXCLUDED.step, payload = EXCLUDED.payload,
                       last_update_id = COALESCE(EXCLUDED.last_update_id, bot_dialogs.last_update_id),
                       updated_at = now()""",
                (self.owner, step, json.dumps(payload), update_id),
            )

    def last_update_id(self) -> int | None:
        return self._dialog()[2]

    # ---- входная точка
    def handle_update(self, update: dict) -> None:
        update_id = update.get("update_id")
        message, callback = update.get("message"), update.get("callback_query")
        source = message or (callback or {}).get("message") or {}
        chat_id = (source.get("chat") or {}).get("id")
        if chat_id != self.owner:
            self.log(f"игнорирую обновление из чужого чата (update_id {update_id})")
            return
        last = self.last_update_id()
        if last is not None and update_id is not None and update_id <= last:
            return
        if callback:
            self._on_callback(callback, update_id)
        elif message and isinstance(message.get("text"), str):
            self._on_text(message["text"], update_id)
        if update_id is not None:
            step, payload, _ = self._dialog()
            self._set_dialog(step, payload, update_id)

    # ---- текст
    def _on_text(self, text: str, update_id: int | None) -> None:
        text = text.strip()
        step, payload, _ = self._dialog()
        if text in ("/start", "/menu"):
            self.api.send(self.owner, "Выберите действие", MENU)
        elif text in ("/cancel", CANCEL):
            self._set_dialog(None, {})
            self.api.send(self.owner, "Отменено", MENU)
        elif text == MENU_ADD:
            self._set_dialog("alert_id", {})
            self.api.send(self.owner, "Введите ID оповещения")
        elif text == MENU_REPORT:
            self._set_dialog(None, {})
            self.api.send(self.owner, "Выберите период", REPORT_KEYBOARD)
        elif step == "alert_id":
            self._step_alert_id(text)
        elif step == "market":
            self.api.send(self.owner, "Выберите рынок кнопкой: ТБ, ИТБ1 или ИТБ2", MARKET_KEYBOARD)
        elif step == "line":
            self._step_line(text, payload)
        elif step == "amount":
            self._step_amount(text, payload)
        elif step == "odds":
            self._step_odds(text, payload, update_id)
        else:
            self.api.send(self.owner, "Начните с меню", MENU)

    def _step_alert_id(self, text: str) -> None:
        if not INTEGER.match(text):
            self.api.send(self.owner, "ID — целое число. Введите ID оповещения")
            return
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT alert_id, fixture_id, home_name, away_name, kickoff_at FROM live_alerts_sent WHERE alert_id = %s",
                (int(text),),
            )
            row = cur.fetchone()
        if row is None:
            self.api.send(self.owner, "Такого ID нет. Введите ID оповещения")
            return
        alert_id, fixture_id, home, away, kickoff = row
        payload = {"alert_id": alert_id, "fixture_id": fixture_id, "kickoff_at": kickoff.isoformat() if kickoff else None}
        self._set_dialog("market", payload)
        self.api.send(self.owner, f"Матч: {home} — {away} (ID {alert_id}). Выберите рынок", MARKET_KEYBOARD)

    def _step_line(self, text: str, payload: dict) -> None:
        value = parse_number(text)
        if value is None or value % QUARTER != 0 or not (QUARTER <= value <= 20):
            self.api.send(self.owner, "Линия — число, кратное 0.25, от 0.25 до 20, например 2.5. Введите число")
            return
        self._set_dialog("amount", {**payload, "line": str(value.quantize(Decimal("0.01")))})
        self.api.send(self.owner, "Введите сумму ставки")

    def _step_amount(self, text: str, payload: dict) -> None:
        value = parse_number(text)
        if value is None or not (0 < value <= 1_000_000):
            self.api.send(self.owner, "Сумма — положительное число, не больше 1000000. Введите сумму ставки")
            return
        self._set_dialog("odds", {**payload, "amount": str(value.quantize(Decimal("0.01"), ROUND_HALF_UP))})
        self.api.send(self.owner, "Введите коэффициент")

    def _step_odds(self, text: str, payload: dict, update_id: int | None) -> None:
        value = parse_number(text)
        if value is None or not (1 < value <= 1000):
            self.api.send(self.owner, "Коэффициент — число больше 1, не больше 1000. Введите коэффициент")
            return
        odds = value.quantize(Decimal("0.001"), ROUND_HALF_UP)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO bets (alert_id, fixture_id, market, line, odds, stake, kickoff_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING bet_id""",
                    (payload["alert_id"], payload["fixture_id"], payload["market"], payload["line"], odds,
                     payload["amount"], payload.get("kickoff_at")),
                )
                bet_id = cur.fetchone()[0]
            self._set_dialog(None, {}, update_id)
        self.api.send(
            self.owner,
            f"Записано: ставка №{bet_id}, оповещение {payload['alert_id']}, {MARKET_TITLES[payload['market']]} "
            f"{payload['line']}, сумма {payload['amount']}, кф {odds}",
            MENU,
        )

    # ---- кнопки
    def _on_callback(self, callback: dict, update_id: int | None) -> None:
        data = callback.get("data") or ""
        step, payload, _ = self._dialog()
        try:
            self.api.answer_callback(callback["id"])
        except ApiError:
            pass
        if data == "cancel":
            self._set_dialog(None, {})
            self.api.send(self.owner, "Отменено", MENU)
        elif data.startswith("mkt:") and step == "market" and data[4:] in MARKET_TITLES:
            self._set_dialog("line", {**payload, "market": data[4:]})
            self.api.send(self.owner, "Введите число (линия), например 2.5")
        elif data in ("rep:day", "rep:month"):
            mode = data[4:]
            self.api.send(self.owner, format_report(report_rows(self.conn, mode), mode), html=True)
        else:
            self.api.send(self.owner, "Начните с меню", MENU)


def main() -> int:
    url = database_url()
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1
    api = TelegramApi(token, os.environ.get("TELEGRAM_PROXY") or None)
    print("бот реестра ставок запущен", flush=True)
    while True:
        try:
            with connect(url) as conn:
                bot = Bot(api, conn, int(chat), log=lambda m: print(m, flush=True))
                offset = (bot.last_update_id() or 0) + 1
                while True:
                    for update in api.get_updates(offset):
                        try:
                            bot.handle_update(update)
                        except ApiError as exc:
                            print(f"ошибка Telegram при обработке: {exc}", flush=True)
                        except Exception as exc:  # одна плохая запись не должна ронять бота
                            print(f"ошибка обработки обновления: {type(exc).__name__}", flush=True)
                        offset = update["update_id"] + 1
        except ApiError as exc:
            print(f"Telegram недоступен: {exc}", flush=True)
        except Exception as exc:
            print(f"сбой: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
