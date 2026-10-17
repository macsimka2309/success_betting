"""Тесты учёта проходимости оповещений: гол после 75-й минуты (specs/онлайн-оповещения.md)."""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from src.bot import registry_bot as rb
from src.jobs import settle_bets as sb
from tests.test_collect import conn, make_client  # noqa: F401  (conn — фикстура)
from tests.test_registry_bot import FakeApi, Updates, OWNER

DB = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


def goal(minute, detail="Normal Goal", team=1):
    return {"type": "Goal", "detail": detail, "time": {"elapsed": minute, "extra": None}, "team": {"id": team}}


def finished(fid, home, away, events, status="FT"):
    return {"fixture": {"id": fid, "status": {"short": status}}, "score": {"fulltime": {"home": home, "away": away}}, "events": events}


# ------------------------------------------------- чистые функции (база не нужна)


@pytest.mark.parametrize(
    "events,expected",
    [
        ([], (0, 0)),
        ([goal(75)], (1, 0)),            # ровно 75-я минута — ещё не «после»
        ([goal(76)], (1, 1)),
        ([goal(90)], (1, 1)),            # 90+N в источнике: elapsed 90
        ([goal(30), goal(80), goal(88)], (3, 2)),
        ([goal(91)], (0, 0)),            # дополнительное время не входит
        ([goal(118, "Penalty")], (0, 0)),
        ([goal(80, "Missed Penalty")], (0, 0)),
        ([goal(80, "Own Goal")], (1, 1)),
        ([goal(80, "Penalty")], (1, 1)),
        ([{"type": "Card", "detail": "Yellow Card", "time": {"elapsed": 80}}], (0, 0)),
    ],
)
def test_count_regulation_goals(events, expected):
    assert sb.count_regulation_goals(events) == expected


def test_goal_without_minute_cannot_be_evaluated():
    assert sb.count_regulation_goals([{"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": None}}]) is None


def test_classify_hit_and_miss():
    assert sb.classify_alert(finished(1, 2, 1, [goal(10), goal(50), goal(88)])) == ("hit", 1, 2, 1)
    assert sb.classify_alert(finished(1, 1, 1, [goal(10), goal(50)])) == ("miss", 0, 1, 1)
    assert sb.classify_alert(finished(1, 0, 0, [])) == ("miss", 0, 0, 0)


def test_classify_incomplete_events_is_no_data():
    assert sb.classify_alert(finished(1, 3, 0, [goal(10)])) == ("no_data", None, 3, 0)
    assert sb.classify_alert(finished(1, 1, 0, [goal(10), goal(20)])) == ("no_data", None, 1, 0)


def test_classify_extra_time_uses_regulation_only():
    item = finished(1, 1, 1, [goal(20), goal(60), goal(100)], status="AET")
    assert sb.classify_alert(item) == ("miss", 0, 1, 1)


@pytest.mark.parametrize("status", ["PST", "CANC", "ABD", "AWD", "WO"])
def test_classify_void(status):
    assert sb.classify_alert({"fixture": {"id": 1, "status": {"short": status}}, "score": {}, "events": []})[0] == "void"


@pytest.mark.parametrize("status", ["NS", "1H", "HT", "2H", "ET", "LIVE"])
def test_classify_unfinished_is_none(status):
    assert sb.classify_alert(finished(1, 0, 0, [], status=status)) is None
    assert sb.classify_alert(None) is None


def test_classify_missing_score_is_no_data():
    item = finished(1, None, None, [])
    assert sb.classify_alert(item) == ("no_data", None, None, None)


# ------------------------------------------------- задание (нужна база)


def add_alert(conn, fixture_id, category="stats", minutes_ago=120):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO live_alerts_sent (fixture_id, category, minute, goals_home, goals_away, possession, sent_at) "
            "VALUES (%s, %s, 65, 0, 0, 60, now() - make_interval(mins => %s)) RETURNING alert_id",
            (fixture_id, category, minutes_ago),
        )
        return cur.fetchone()[0]


def outcome(conn, alert_id):
    with conn.cursor() as cur:
        cur.execute("SELECT outcome, goals_after_75, final_goals_home, final_goals_away, resolved_at IS NOT NULL "
                    "FROM live_alerts_sent WHERE alert_id = %s", (alert_id,))
        return cur.fetchone()


@DB
def test_job_resolves_hit_and_miss(tmp_path, conn):
    a, b = add_alert(conn, 1), add_alert(conn, 2)
    client = make_client(tmp_path, [{"response": [finished(1, 1, 0, [goal(80)]), finished(2, 0, 1, [goal(20)])]}])

    ctx = sb.run_settle(client, conn)

    assert outcome(conn, a) == ("hit", 1, 1, 0, True)
    assert outcome(conn, b) == ("miss", 0, 0, 1, True)
    assert ctx.items_processed == 2 and client.requests_used == 1


@DB
def test_job_leaves_running_match_pending(tmp_path, conn):
    a = add_alert(conn, 1)
    client = make_client(tmp_path, [{"response": [finished(1, 0, 0, [], status="2H")]}])

    sb.run_settle(client, conn)

    assert outcome(conn, a)[0] == "pending"


@DB
def test_young_alert_is_not_fetched(tmp_path, conn):
    add_alert(conn, 1, minutes_ago=10)
    client = make_client(tmp_path, [])

    sb.run_settle(client, conn)

    assert client.requests_used == 0


@DB
def test_alert_and_bet_share_one_request(tmp_path, conn):
    a = add_alert(conn, 1)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO bets (fixture_id, market, line, stake, odds) VALUES (1, 'TB', 0.5, 100, 2)")
    client = make_client(tmp_path, [{"response": [finished(1, 1, 0, [goal(85)])]}])

    sb.run_settle(client, conn)

    assert client.requests_used == 1 and outcome(conn, a)[0] == "hit"
    with conn.cursor() as cur:
        cur.execute("SELECT status, profit FROM bets")
        assert cur.fetchone()[0] == "won"


@DB
def test_resolved_alert_is_not_touched_again(tmp_path, conn):
    a = add_alert(conn, 1)
    sb.run_settle(make_client(tmp_path, [{"response": [finished(1, 1, 0, [goal(80)])]}]), conn)
    second = make_client(tmp_path / "b", [])

    sb.run_settle(second, conn)

    assert second.requests_used == 0 and outcome(conn, a)[0] == "hit"


@DB
def test_void_and_no_data_alerts(tmp_path, conn):
    v, n = add_alert(conn, 1), add_alert(conn, 2)
    bodies = [{"response": [{"fixture": {"id": 1, "status": {"short": "PST"}}, "score": {}, "events": []},
                            finished(2, 2, 0, [goal(10)])]}]
    sb.run_settle(make_client(tmp_path, bodies), conn)

    assert outcome(conn, v)[0] == "void" and outcome(conn, n)[0] == "no_data"


@DB
def test_hit_rate_view_counts_only_evaluated(conn):
    for fid, category, state in ((1, "stats", "hit"), (2, "stats", "miss"), (3, "stats", "no_data"),
                                 (4, "stats", "pending"), (5, "half", "hit"), (6, "half", "void")):
        alert_id = add_alert(conn, fid, category)
        with conn.cursor() as cur:
            cur.execute("UPDATE live_alerts_sent SET outcome = %s WHERE alert_id = %s", (state, alert_id))

    with conn.cursor() as cur:
        cur.execute("SELECT category, alerts, resolved, hits, hit_percent, pending FROM alert_hit_rate ORDER BY category")
        rows = cur.fetchall()
    assert [(r[0], r[1], r[2], r[3], float(r[4]), r[5]) for r in rows] == [("half", 2, 1, 1, 100.0, 0), ("stats", 4, 2, 1, 50.0, 1)]


# ------------------------------------------------- кнопка бота


def test_format_hit_rate_table():
    text = rb.format_hit_rate([("stats", 10, 8, 4, 2), ("half", 3, 3, 1, 0)])
    lines = text.replace("<pre>", "").replace("</pre>", "").splitlines()
    assert lines[1].split() == ["Владение+удары", "10", "8", "4", "50"]
    assert lines[2].split()[-1] == "33"
    assert lines[3].split() == ["Итого", "13", "11", "5", "45"]
    assert "Ожидают конца матча: 2" in text and "после 75-й" in text


def test_format_hit_rate_without_evaluated_alerts():
    text = rb.format_hit_rate([("stats", 2, 0, 0, 2)])
    assert " - " in text.replace("<pre>", "") or text.splitlines()[1].split()[-1] == "-"
    assert rb.format_hit_rate([]) == "Оповещений пока нет"


@DB
def test_bot_button_shows_hit_rate(conn):
    api, up = FakeApi(), Updates()
    bot = rb.Bot(api, conn, OWNER, log=lambda _: None)
    alert_id = add_alert(conn, 1)
    with conn.cursor() as cur:
        cur.execute("UPDATE live_alerts_sent SET outcome = 'hit' WHERE alert_id = %s", (alert_id,))

    bot.handle_update(up.text(rb.MENU_HITS))

    assert api.sent[-1][3] is True and "Владение+удары" in api.last and "Итого" in api.last


def test_menu_has_hit_rate_button():
    labels = [b["text"] for row in rb.MENU["keyboard"] for b in row]
    assert rb.MENU_HITS in labels
