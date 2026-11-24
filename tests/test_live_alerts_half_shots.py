"""Категория half_shots, минуты 65–80: по таймам владение >= 70 % и 3+ удара у команды или 4+ суммарно (specs/онлайн-оповещения.md)."""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from src.jobs import live_alerts as la
from tests.test_collect import conn, make_client  # noqa: F401  (conn — фикстура)
from tests.test_live_alerts import Outbox, live_item

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


def ex(h_poss, h_shots, a_poss=None, a_shots=0):
    return {10: {"possession": h_poss, "shots": h_shots}, 20: {"possession": a_poss if a_poss is not None else 100 - h_poss, "shots": a_shots}}


def item(minute=70, home=0, away=1, status="2H"):
    return live_item(1, minute=minute, home=home, away=away, status=status)


@pytest.mark.parametrize("poss,shots,expected", [(71, 3, True), (70, 3, True), (69, 5, False), (80, 1, False), (100, 9, True)])
def test_halves_possession_70_or_more_and_two_shots(poss, shots, expected):
    assert (la.build_half_shots_alert(item(), ex(poss, shots), {}) is not None) == expected


def test_either_team_and_message():
    alert = la.build_half_shots_alert(item(), ex(25, 0, 75, 4), {"home": 40, "away": 60})
    assert alert.team_side == "away" and alert.shots == 4 and alert.opponent_shots == 0
    text = la.format_message(alert)
    assert "Тип: владение и удары во 2-м тайме" in text and "владение 75 %, удары в створ 4" in text


@pytest.mark.parametrize("goals", [(0, 1), (3, 1), (0, 4)])
def test_lead_size_is_not_a_condition(goals):
    assert la.build_half_shots_alert(item(home=goals[0], away=goals[1]), ex(75, 3), {}) is not None


@pytest.mark.parametrize("minute,expected", [(64, False), (65, True), (75, True), (80, False), (81, False)])
def test_minute_window(minute, expected):
    assert (la.build_half_shots_alert(item(minute=minute), ex(75, 4), {}) is not None) == expected


def exact_body(poss, shots):
    def block(p, s):
        return [{"type": "Ball Possession", "value": f"{p}%"}, {"type": "Shots on Goal", "value": s}]
    return {"response": [
        {"team": {"id": 10}, "statistics_2h": block(poss, shots)},
        {"team": {"id": 20}, "statistics_2h": block(100 - poss, 0)},
    ]}


def run(tmp_path, conn, bodies):
    client = make_client(tmp_path, bodies, max_requests=10)
    outbox = Outbox()
    la.run_live_alerts(client, conn, outbox, categories=("half_shots",), log=lambda _: None)
    return client, outbox


def test_sends_once_using_only_second_half_stats(tmp_path, conn):
    client, outbox = run(tmp_path, conn, [{"response": [item()]}, exact_body(78, 3)])
    assert len(outbox.messages) == 1 and client.requests_used == 2
    client, outbox = run(tmp_path / "b", conn, [{"response": [item()]}, exact_body(78, 3)])
    assert outbox.messages == [] and client.requests_used == 2


@pytest.mark.parametrize("goals", [(0, 0), (1, 1), (2, 2)])
def test_draws_are_not_taken(tmp_path, conn, goals):
    assert la.build_half_shots_alert(item(home=goals[0], away=goals[1]), ex(80, 5), {}) is None


def test_draw_still_collects_statistics(tmp_path, conn):
    draw = live_item(1, minute=70, home=1, away=1)
    client, outbox = run(tmp_path, conn, [{"response": [draw]}, exact_body(50, 0)])
    assert outbox.messages == [] and client.requests_used == 2


def test_only_half_shots_is_enabled_in_production():
    assert la.ENABLED_CATEGORIES == ("half_pressure",)


def stored_sources(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT stats_source, shots_on_goal, opponent_shots_on_goal FROM live_alerts_sent")
        return cur.fetchall()


def test_source_half_is_stored_with_second_half_shots(tmp_path, conn):
    run(tmp_path, conn, [{"response": [item()]}, exact_body(78, 4)])
    assert stored_sources(conn) == [("half", 4, 0)]
