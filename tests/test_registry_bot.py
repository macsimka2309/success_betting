"""Тесты бота реестра ставок (specs/реестр-ставок.md). Telegram подменён, база нужна."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

psycopg = pytest.importorskip("psycopg")

from src.bot import registry_bot as rb
from src.bot.telegram_api import ApiError, TelegramApi
from tests.test_collect import conn  # noqa: F401  (conn — фикстура)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)

OWNER = 111
FOREIGN = 999


class FakeApi:
    def __init__(self):
        self.sent = []  # (chat_id, text, reply_markup, html)
        self.answered = []

    def send(self, chat_id, text, reply_markup=None, html=False):
        self.sent.append((chat_id, text, reply_markup, html))

    def answer_callback(self, callback_id):
        self.answered.append(callback_id)

    @property
    def last(self):
        return self.sent[-1][1]


class Updates:
    def __init__(self):
        self.n = 0

    def text(self, text, chat=OWNER):
        self.n += 1
        return {"update_id": self.n, "message": {"chat": {"id": chat}, "text": text}}

    def button(self, data, chat=OWNER):
        self.n += 1
        return {"update_id": self.n, "callback_query": {"id": f"cb{self.n}", "data": data, "message": {"chat": {"id": chat}}}}


@pytest.fixture()
def env(conn):
    api, up = FakeApi(), Updates()
    bot = rb.Bot(api, conn, OWNER, log=lambda _: None)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO live_alerts_sent (fixture_id, home_name, away_name, minute, goals_home, goals_away, "
            "possession, shots_on_goal, kickoff_at) VALUES (500, 'Home FC', 'Away FC', 67, 0, 1, 58, 7, now()) RETURNING alert_id"
        )
        alert_id = cur.fetchone()[0]
    return bot, api, up, conn, alert_id


def feed(bot, up, *steps):
    for step in steps:
        bot.handle_update(up.button(step[1:]) if step.startswith("@") else up.text(step))


def bets(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT alert_id, fixture_id, market, line, stake, odds, status FROM bets ORDER BY bet_id")
        return cur.fetchall()


# ------------------------------------------------------------ основной диалог


def test_full_dialog_creates_bet(env):
    bot, api, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", "1.5", "250", "1.85")

    assert bets(conn) == [(alert_id, 500, "TB", D("1.50"), D("250.00"), D("1.850"), "open")]
    assert "Записано" in api.last and "ТБ" in api.last and "1.50" in api.last
    with conn.cursor() as cur:
        cur.execute("SELECT kickoff_at IS NOT NULL FROM bets")
        assert cur.fetchone()[0] is True  # время матча взято из оповещения


def test_prompts_in_order(env):
    bot, api, up, _, alert_id = env
    feed(bot, up, rb.MENU_ADD)
    assert "ID оповещения" in api.last
    feed(bot, up, str(alert_id))
    assert "Home FC — Away FC" in api.last and api.sent[-1][2] is rb.MARKET_KEYBOARD
    feed(bot, up, "@mkt:TB")
    assert "линия" in api.last
    feed(bot, up, "2.5")
    assert "сумму" in api.last
    feed(bot, up, "100")
    assert "коэффициент" in api.last


def test_comma_decimal_is_accepted(env):
    bot, _, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", "2,5", "100,50", "1,9")

    assert bets(conn)[0][3:6] == (D("2.50"), D("100.50"), D("1.900"))


def test_several_bets_on_one_alert(env):
    bot, _, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", "2.5", "100", "1.9",
         rb.MENU_ADD, str(alert_id), "@mkt:TB", "0.5", "50", "1.5")

    assert len(bets(conn)) == 2


# ------------------------------------------------------------ ошибки ввода


@pytest.mark.parametrize("bad", ["abc", "-1", "1e3", "", "0x10", "1..5", "٣", "²", "12345678901"])
def test_bad_alert_id_asks_again(env, bad):
    bot, api, up, _, _ = env
    feed(bot, up, rb.MENU_ADD, bad)
    assert "ID" in api.last


def test_unknown_alert_id(env):
    bot, api, up, _, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id + 1000))
    assert "Такого ID нет" in api.last


@pytest.mark.parametrize("bad", ["2.3", "0", "0.2", "21", "abc", "2.5.1", "-2.5"])
def test_bad_line_asks_again(env, bad):
    bot, api, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", bad)
    assert "кратное 0.25" in api.last and bets(conn) == []


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "1000001", "nan"])
def test_bad_amount_asks_again(env, bad):
    bot, api, up, _, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", "2.5", bad)
    assert "Сумма" in api.last


@pytest.mark.parametrize("bad", ["1", "0.9", "1001", "abc"])
def test_bad_odds_asks_again(env, bad):
    bot, api, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", "2.5", "100", bad)
    assert "Коэффициент" in api.last and bets(conn) == []


def test_boundary_values_accepted(env):
    bot, _, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", "0.25", "1000000", "1.001",
         rb.MENU_ADD, str(alert_id), "@mkt:TB", "20", "0.01", "1000")

    assert [b[3:6] for b in bets(conn)] == [(D("0.25"), D("1000000.00"), D("1.001")), (D("20.00"), D("0.01"), D("1000.000"))]


def test_only_total_over_button_is_offered(env):
    buttons = [b["text"] for row in rb.MARKET_KEYBOARD["inline_keyboard"] for b in row]
    assert buttons == ["ТБ", "Отмена"]


@pytest.mark.parametrize("old", ["ITB1", "ITB2", "XYZ"])
def test_removed_markets_are_rejected_even_from_old_keyboards(env, old):
    bot, api, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), f"@mkt:{old}")

    assert bets(conn) == [] and "меню" in api.last
    feed(bot, up, "2.5")  # диалог остался на шаге выбора рынка, линия не принимается
    assert "кнопкой" in api.last


def test_typing_instead_of_pressing_market_button(env):
    bot, api, up, _, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "ТБ")
    assert "кнопкой" in api.last


def test_button_without_active_dialog(env):
    bot, api, up, conn, _ = env
    feed(bot, up, "@mkt:TB")
    assert "меню" in api.last and bets(conn) == []


def test_free_text_without_dialog(env):
    bot, api, up, _, _ = env
    feed(bot, up, "привет")
    assert "меню" in api.last


def test_cancel_resets_dialog(env):
    bot, api, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", "/cancel", "2.5")
    assert "Отменено" in api.sent[-2][1] and "меню" in api.last and bets(conn) == []


def test_cancel_button(env):
    bot, api, up, _, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@cancel")
    assert "Отменено" in api.last


# ------------------------------------------------------------ доступ и устойчивость


def test_foreign_chat_is_ignored(env):
    bot, api, up, conn, alert_id = env
    bot.handle_update(up.text(rb.MENU_ADD, chat=FOREIGN))
    bot.handle_update(up.button("rep:day", chat=FOREIGN))

    assert api.sent == [] and api.answered == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM bot_dialogs")
        assert cur.fetchone()[0] == 0


def test_dialog_survives_restart(env):
    bot, api, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", "2.5")
    restarted = rb.Bot(api, conn, OWNER, log=lambda _: None)

    restarted.handle_update(up.text("100"))
    restarted.handle_update(up.text("1.9"))

    assert len(bets(conn)) == 1


def test_redelivered_update_is_ignored(env):
    bot, api, up, conn, alert_id = env
    feed(bot, up, rb.MENU_ADD, str(alert_id), "@mkt:TB", "2.5", "100")
    final = up.text("1.9")
    bot.handle_update(final)
    bot.handle_update(final)  # Telegram прислал то же обновление ещё раз

    assert len(bets(conn)) == 1


def test_last_update_id_is_remembered(env):
    bot, _, up, _, _ = env
    feed(bot, up, "/start", "/start")
    assert bot.last_update_id() == 2


# ------------------------------------------------------------ отчёт


def msk_now():
    return datetime.now(timezone.utc) + timedelta(hours=3)


def add_settled(conn, kickoff, profit, status="won"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bets (fixture_id, market, line, odds, stake, kickoff_at, status, profit) "
            "VALUES (1, 'TB', 2.5, 1.9, 100, %s, %s, %s)",
            (kickoff, status, profit),
        )


def test_report_by_day(env):
    bot, api, up, conn, _ = env
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    add_settled(conn, today, 90)
    add_settled(conn, today, -100, "lost")
    add_settled(conn, today - timedelta(days=1), 45, "half_won")

    feed(bot, up, rb.MENU_REPORT, "@rep:day")

    text = api.last
    assert api.sent[-1][3] is True and text.startswith("<pre>")
    day = (today + timedelta(hours=3)).strftime("%Y-%m-%d")
    prev = (today + timedelta(hours=3) - timedelta(days=1)).strftime("%Y-%m-%d")
    assert f"{day}" in text and f"{prev}" in text
    lines = text.replace("<pre>", "").replace("</pre>", "").splitlines()
    assert lines[0].split() == ["День", "Ставок", "Результат"]
    assert lines[1].split() == [day, "2", "-10.00"]
    assert lines[2].split() == [prev, "1", "+45.00"]
    assert lines[3].split() == ["Итого", "3", "+35.00"]


def test_report_by_month_and_pending_counter(env):
    bot, api, up, conn, _ = env
    now = datetime.now(timezone.utc).replace(hour=12)
    add_settled(conn, now, 100)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bets (fixture_id, market, line, odds, stake, kickoff_at) VALUES (2, 'TB', 2.5, 1.9, 10, %s)", (now,)
        )

    feed(bot, up, "@rep:month")

    month = (now + timedelta(hours=3)).strftime("%Y-%m")
    assert "Месяц" in api.last and month in api.last and "Не рассчитано: 1" in api.last


def test_report_excludes_old_days_but_keeps_month_window(env):
    bot, api, up, conn, _ = env
    old = datetime.now(timezone.utc) - timedelta(days=40)
    add_settled(conn, old, 500)

    feed(bot, up, "@rep:day")
    assert "нет" in api.last
    feed(bot, up, "@rep:month")
    assert old.strftime("%Y-%m") in api.last  # 40 дней назад ещё в окне 12 месяцев


def test_report_without_bets(env):
    bot, api, up, _, _ = env
    feed(bot, up, "@rep:day")
    assert "Ставок за период нет" in api.last


def test_report_groups_by_moscow_match_day(env):
    """Матч в 22:30 UTC — уже следующий день по Москве."""
    bot, api, up, conn, _ = env
    base = datetime.now(timezone.utc).replace(hour=22, minute=30, second=0, microsecond=0) - timedelta(days=2)
    add_settled(conn, base, 10)

    feed(bot, up, "@rep:day")

    assert (base + timedelta(hours=3)).strftime("%Y-%m-%d") in api.last


# ------------------------------------------------------------ Telegram API


def test_api_never_puts_token_in_argv_or_errors():
    seen = {}

    class Result:
        returncode, stdout = 0, '{"ok": false, "error_code": 403, "description": "Forbidden"}'

    def runner(argv, stdin, timeout):
        seen["argv"], seen["stdin"] = argv, stdin
        return Result()

    api = TelegramApi("123:SECRET", "socks5h://127.0.0.1:1080", runner=runner)
    with pytest.raises(ApiError) as info:
        api.send(OWNER, 'текст "с кавычкой"', rb.MENU, html=True)

    assert not any("SECRET" in a for a in seen["argv"]) and "SECRET" not in str(info.value)
    assert "bot123:SECRET/sendMessage" in seen["stdin"] and "parse_mode=HTML" in seen["stdin"]
    assert "socks5h://127.0.0.1:1080" in seen["argv"]


def test_parse_number():
    assert rb.parse_number("2,5") == D("2.5")
    assert rb.parse_number(" 10 ") == D(10)
    for bad in ("1e3", "-1", "nan", "inf", "", "1.2.3", "٣"):
        assert rb.parse_number(bad) is None
