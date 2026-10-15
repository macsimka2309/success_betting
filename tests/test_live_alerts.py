"""Тесты оповещений по идущим матчам (specs/онлайн-оповещения.md).

Транспорт API и отправитель подменены, настоящих запросов нет (НФТ-8).
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from src.jobs import live_alerts as la
from tests.test_collect import conn, make_client  # noqa: F401  (conn — фикстура)

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


def live_item(fid, minute=67, home=0, away=1, status="2H"):
    return {
        "fixture": {"id": fid, "status": {"short": status, "elapsed": minute}},
        "league": {"id": 307, "name": "Pro League", "country": "Saudi-Arabia"},
        "teams": {"home": {"id": 10, "name": "Home FC"}, "away": {"id": 20, "name": "Away FC"}},
        "goals": {"home": home, "away": away},
    }


def detail_item(fid, possession="58%", shots_home=7, shots_away=2, **kwargs):
    item = live_item(fid, **kwargs)
    item["statistics"] = [
        {"team": {"id": 10}, "statistics": [
            {"type": "Ball Possession", "value": possession},
            {"type": "Shots on Goal", "value": shots_home},
        ]},
        {"team": {"id": 20}, "statistics": [{"type": "Shots on Goal", "value": shots_away}]},
    ]
    return item


class Outbox:
    def __init__(self, fail_times=0):
        self.messages = []
        self.fail_times = fail_times

    def __call__(self, text):
        if self.fail_times:
            self.fail_times -= 1
            raise la.TelegramError("HTTP 500")
        self.messages.append(text)


def run(tmp_path, conn, bodies, outbox=None, **kwargs):
    client = make_client(tmp_path, bodies, max_requests=kwargs.pop("max_requests", 10))
    outbox = outbox if outbox is not None else Outbox()
    ctx = la.run_live_alerts(client, conn, outbox, log=lambda _: None, **kwargs)
    return client, outbox, ctx


# ---------------------------------------------------------- триггер (АП-1)


@pytest.mark.parametrize(
    "minute,home,away,possession,shots,expected",
    [
        (59, 0, 1, "60%", 7, False),
        (60, 0, 1, "60%", 7, True),
        (75, 0, 1, "60%", 7, True),
        (76, 0, 1, "60%", 7, False),
        (67, 1, 1, "60%", 7, True),    # ничья
        (67, 0, 2, "60%", 7, True),    # проигрыш
        (67, 2, 1, "60%", 7, False),   # хозяева впереди
        (67, 0, 1, "55%", 7, False),   # ровно 55 — не «более 55»
        (67, 0, 1, "56%", 7, True),
        (67, 0, 1, "60%", 5, False),
        (67, 0, 1, "60%", 6, True),
    ],
)
def test_trigger_boundaries(minute, home, away, possession, shots, expected):
    alert = la.build_alert(detail_item(1, possession=possession, shots_home=shots, minute=minute, home=home, away=away))
    assert (alert is not None) == expected


@pytest.mark.parametrize("status", ["HT", "ET", "NS", "FT", "PEN"])
def test_only_first_and_second_half_statuses_count(status):
    assert la.build_alert(detail_item(1, status=status)) is None


@pytest.mark.parametrize("possession,shots", [(None, 7), ("58%", None), ("n/a", 7), ("58%", "x")])
def test_missing_or_garbage_stats_do_not_alert(possession, shots):
    assert la.build_alert(detail_item(1, possession=possession, shots_home=shots)) is None


def test_no_statistics_block_does_not_alert():
    assert la.build_alert(live_item(1)) is None


def test_elapsed_none_is_not_a_candidate():
    assert la.is_candidate(live_item(1, minute=None)) is False


def test_message_contains_numbers_and_unknown_away_shots():
    item = detail_item(1)
    item["statistics"][1]["statistics"] = []
    text = la.format_message(la.build_alert(item))
    assert "Home FC — Away FC" in text and "67'" in text and "0:1" in text
    assert "58 %" in text and "хозяева 7, гости ?" in text


# --------------------------------------------------- запуск, дубли, повторы


def test_matching_match_sends_one_message_and_records_it(tmp_path, conn):
    bodies = [{"response": [live_item(1), live_item(2, minute=30)]}, {"response": [detail_item(1)]}]
    client, outbox, ctx = run(tmp_path, conn, bodies)

    assert len(outbox.messages) == 1 and "Home FC" in outbox.messages[0]
    assert client.requests_used == 2
    assert "ids=1" in client.transport.calls[1] and "live=all" in client.transport.calls[0]
    with conn.cursor() as cur:
        cur.execute("SELECT fixture_id, possession_home, shots_on_goal_home FROM live_alerts_sent")
        assert cur.fetchall() == [(1, 58, 7)]


def test_same_match_in_next_run_is_not_sent_again(tmp_path, conn):
    run(tmp_path, conn, [{"response": [live_item(1)]}, {"response": [detail_item(1)]}])

    client, outbox, _ = run(tmp_path / "b", conn, [{"response": [live_item(1)]}])

    assert outbox.messages == []
    assert client.requests_used == 1  # статистику уже отправленного матча не запрашиваем


def test_failed_send_is_retried_next_run(tmp_path, conn):
    bodies = [{"response": [live_item(1)]}, {"response": [detail_item(1)]}]
    flaky = Outbox(fail_times=1)
    with pytest.raises(RuntimeError, match="не отправлено"):
        run(tmp_path, conn, bodies, outbox=flaky)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM live_alerts_sent")
        assert cur.fetchone()[0] == 0

    _, outbox, _ = run(tmp_path / "b", conn, bodies)

    assert len(outbox.messages) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM collection_runs WHERE job_name='live_alerts' ORDER BY run_id")
        assert [r[0] for r in cur.fetchall()] == ["failed", "success"]


def test_dry_run_prints_and_records_nothing(tmp_path, conn):
    logged = []
    client = make_client(tmp_path, [{"response": [live_item(1)]}, {"response": [detail_item(1)]}])
    la.run_live_alerts(client, conn, None, dry_run=True, log=logged.append)

    assert len(logged) == 1 and "[dry-run]" in logged[0]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM live_alerts_sent")
        assert cur.fetchone()[0] == 0


def test_match_without_statistics_is_skipped_quietly(tmp_path, conn):
    client, outbox, ctx = run(tmp_path, conn, [{"response": [live_item(1)]}, {"response": [live_item(1)]}])

    assert outbox.messages == [] and ctx.skipped == 1 and ctx.items_processed == 0


def test_no_live_matches_costs_one_request(tmp_path, conn):
    client, outbox, _ = run(tmp_path, conn, [{"response": []}])

    assert client.requests_used == 1 and outbox.messages == []


# ----------------------------------------------------------- бюджет (АП-4)


@pytest.mark.parametrize("candidates,requests", [(1, 2), (20, 2), (21, 3), (40, 3), (41, 4)])
def test_requests_are_batched_by_twenty(tmp_path, conn, candidates, requests):
    ids = list(range(1, candidates + 1))
    bodies = [{"response": [live_item(i) for i in ids]}] + [{"response": []}] * 3
    client, _, _ = run(tmp_path, conn, bodies)

    assert client.requests_used == requests


def test_budget_cap_stops_cleanly(tmp_path, conn):
    ids = list(range(1, 42))
    bodies = [{"response": [live_item(i) for i in ids]}, {"response": []}]
    client = make_client(tmp_path, bodies, max_requests=2)
    ctx = la.run_live_alerts(client, conn, Outbox(), log=lambda _: None)

    assert client.requests_used == 2
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM collection_runs WHERE run_id = %s", (ctx.run_id,))
        assert cur.fetchone()[0] == "quota_exceeded"


def test_api_error_marks_run_failed(tmp_path, conn):
    client = make_client(tmp_path, [{"errors": {"token": "bad"}, "response": []}])
    with pytest.raises(RuntimeError):
        la.run_live_alerts(client, conn, Outbox(), log=lambda _: None)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM collection_runs WHERE job_name='live_alerts'")
        assert cur.fetchone()[0] == "failed"


# ------------------------------------------------------- Telegram (АП-6)


def test_token_is_not_in_curl_argv_but_in_config():
    argv, config = la.telegram_curl_request("123:SECRET", "-100500", 'текст "с кавычкой"\nстрока', "socks5h://127.0.0.1:1080")

    assert not any("SECRET" in a for a in argv)
    assert "bot123:SECRET/sendMessage" in config
    assert "socks5h://127.0.0.1:1080" in argv
    assert '\\"с кавычкой\\"' in config and "\\n" in config


def test_sender_errors_do_not_leak_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100500")

    class Result:
        returncode, stdout, stderr = 0, "403", "curl: https://api.telegram.org/bot123:SECRET/sendMessage"

    monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(la.TelegramError) as info:
        la.telegram_sender_from_env()("привет")

    assert "SECRET" not in str(info.value) and "403" in str(info.value)


def test_sender_requires_token_and_chat(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(SystemExit):
        la.telegram_sender_from_env()
