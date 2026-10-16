"""Тесты задания расчёта ставок (specs/реестр-ставок.md, РС-1…РС-6). Транспорт подменён."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

psycopg = pytest.importorskip("psycopg")

from src.jobs import settle_bets
from tests.test_collect import conn, make_client  # noqa: F401  (conn — фикстура)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


def add_bet(conn, fixture_id, market="TB", line="2.5", stake="100", odds="1.9", kickoff=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bets (fixture_id, market, line, stake, odds, kickoff_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING bet_id",
            (fixture_id, market, line, stake, odds, kickoff or datetime.now(timezone.utc) - timedelta(hours=5)),
        )
        return cur.fetchone()[0]


def fixture(fid, status="FT", home=2, away=1):
    return {"fixture": {"id": fid, "status": {"short": status}}, "score": {"fulltime": {"home": home, "away": away}}}


def bet(conn, bet_id):
    with conn.cursor() as cur:
        cur.execute("SELECT status, goals_home, goals_away, profit FROM bets WHERE bet_id = %s", (bet_id,))
        return cur.fetchone()


def test_finished_match_is_settled(tmp_path, conn):
    bet_id = add_bet(conn, 1)
    client = make_client(tmp_path, [{"response": [fixture(1, home=2, away=1)]}])

    ctx = settle_bets.run_settle(client, conn)

    assert bet(conn, bet_id) == ("won", 2, 1, D("90.000"))
    assert ctx.items_processed == 1 and client.requests_used == 1
    assert "ids=1" in client.transport.calls[0]


def test_lost_and_itb_markets(tmp_path, conn):
    a = add_bet(conn, 1, "TB", "2.5")
    b = add_bet(conn, 1, "ITB2", "0.5")
    client = make_client(tmp_path, [{"response": [fixture(1, home=1, away=1)]}])

    settle_bets.run_settle(client, conn)

    assert bet(conn, a)[0] == "lost" and bet(conn, a)[3] == D("-100.000")
    assert bet(conn, b)[0] == "won" and bet(conn, b)[3] == D("90.000")
    assert client.requests_used == 1  # одна пачка на оба матча-ставки


def test_extra_time_uses_regulation_score(tmp_path, conn):
    bet_id = add_bet(conn, 1, "TB", "2.5")
    item = fixture(1, status="AET", home=1, away=1)  # fulltime 1:1, в допвремя было ещё 2 гола
    client = make_client(tmp_path, [{"response": [item]}])

    settle_bets.run_settle(client, conn)

    assert bet(conn, bet_id)[0] == "lost"


def test_unfinished_match_stays_open(tmp_path, conn):
    bet_id = add_bet(conn, 1)
    client = make_client(tmp_path, [{"response": [fixture(1, status="2H")]}])

    settle_bets.run_settle(client, conn)

    assert bet(conn, bet_id)[0] == "open"


@pytest.mark.parametrize("status", ["PST", "CANC", "ABD", "AWD", "WO"])
def test_cancelled_match_is_void(tmp_path, conn, status):
    bet_id = add_bet(conn, 1)
    client = make_client(tmp_path, [{"response": [fixture(1, status=status, home=None, away=None)]}])

    settle_bets.run_settle(client, conn)

    assert bet(conn, bet_id) == ("void", None, None, D("0.000"))


def test_finished_without_score_stays_open(tmp_path, conn):
    bet_id = add_bet(conn, 1)
    client = make_client(tmp_path, [{"response": [fixture(1, home=None, away=None)]}])

    settle_bets.run_settle(client, conn)

    assert bet(conn, bet_id)[0] == "open"


def test_no_open_bets_costs_no_requests(tmp_path, conn):
    client = make_client(tmp_path, [])

    settle_bets.run_settle(client, conn)

    assert client.requests_used == 0


def test_second_run_does_not_touch_settled_bets(tmp_path, conn):
    bet_id = add_bet(conn, 1)
    settle_bets.run_settle(make_client(tmp_path, [{"response": [fixture(1)]}]), conn)
    before = bet(conn, bet_id)
    second = make_client(tmp_path / "b", [])

    settle_bets.run_settle(second, conn)

    assert bet(conn, bet_id) == before and second.requests_used == 0


def test_batches_of_twenty_and_budget_cap(tmp_path, conn):
    for fid in range(1, 42):
        add_bet(conn, fid)
    client = make_client(tmp_path, [{"response": []}, {"response": []}, {"response": []}], max_requests=2)

    settle_bets.run_settle(client, conn)

    assert client.requests_used == 2


def test_stuck_bet_notifies_once(tmp_path, conn):
    bet_id = add_bet(conn, 1, kickoff=datetime.now(timezone.utc) - timedelta(hours=60))
    sent = []
    bodies = [{"response": [fixture(1, status="2H")]}]
    settle_bets.run_settle(make_client(tmp_path, bodies), conn, notify=sent.append)
    settle_bets.run_settle(make_client(tmp_path / "b", bodies), conn, notify=sent.append)

    assert len(sent) == 1 and f"№{bet_id}" in sent[0]


def test_failed_notification_is_retried(tmp_path, conn):
    add_bet(conn, 1, kickoff=datetime.now(timezone.utc) - timedelta(hours=60))
    bodies = [{"response": [fixture(1, status="2H")]}]

    def broken(_):
        raise RuntimeError("telegram down")

    settle_bets.run_settle(make_client(tmp_path, bodies), conn, notify=broken)
    sent = []
    settle_bets.run_settle(make_client(tmp_path / "b", bodies), conn, notify=sent.append)

    assert len(sent) == 1
