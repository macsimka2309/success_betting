"""Категория half_shots: владение во 2-м тайме > 70 % и 2+ удара в створ (specs/онлайн-оповещения.md)."""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from src.jobs import live_alerts as la
from tests.test_collect import conn, make_client  # noqa: F401  (conn — фикстура)
from tests.test_live_alerts import Outbox, detail_item, live_item

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


def ex(h_poss, h_shots, a_poss=None, a_shots=0):
    return {10: {"possession": h_poss, "shots": h_shots}, 20: {"possession": a_poss if a_poss is not None else 100 - h_poss, "shots": a_shots}}


def item(minute=70, home=0, away=1, status="2H"):
    return live_item(1, minute=minute, home=home, away=away, status=status)


@pytest.mark.parametrize("poss,shots,expected", [(71, 2, True), (70, 5, False), (80, 1, False), (80, 2, True), (100, 9, True)])
def test_possession_strictly_above_70_and_two_shots(poss, shots, expected):
    assert (la.build_half_shots_alert(item(), ex(poss, shots), {}) is not None) == expected


def test_either_team_and_message():
    alert = la.build_half_shots_alert(item(), ex(25, 0, 75, 3), {"home": 40, "away": 60})
    assert alert.team_side == "away" and alert.shots == 3 and alert.opponent_shots == 0
    text = la.format_message(alert)
    assert "Тип: владение и удары во 2-м тайме" in text and "владение 75 %, удары в створ 3" in text


@pytest.mark.parametrize("goals,expected", [((0, 1), True), ((1, 1), True), ((2, 1), True), ((3, 1), False)])
def test_lead_limit(goals, expected):
    assert (la.build_half_shots_alert(item(home=goals[0], away=goals[1]), ex(75, 3), {}) is not None) == expected


@pytest.mark.parametrize("minute,expected", [(64, False), (65, True), (75, True), (76, False)])
def test_minute_window(minute, expected):
    assert (la.build_half_shots_alert(item(minute=minute), ex(75, 3), {}) is not None) == expected


def seed(conn, hp=(55, 45), hs=(1, 1)):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO live_ht_possession (fixture_id, home_possession, away_possession, home_shots_on_goal, away_shots_on_goal) "
            "VALUES (1, %s, %s, %s, %s)", (*hp, *hs))


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


def test_fires_after_prefilter_and_exact_check(tmp_path, conn):
    seed(conn)
    full = detail_item(1, possession="68%", away_possession="32%", shots_home=4, shots_away=1, minute=70, home=0, away=1)
    client, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}, exact_body(78, 3)])
    assert len(outbox.messages) == 1 and "владение и удары во 2-м тайме" in outbox.messages[0]
    assert client.requests_used == 3


def test_not_repeated_for_the_same_match(tmp_path, conn):
    seed(conn)
    full = detail_item(1, possession="68%", away_possession="32%", shots_home=4, shots_away=1, minute=70, home=0, away=1)
    run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}, exact_body(78, 3)])
    client, outbox = run(tmp_path / "b", conn, [{"response": [item()]}])
    assert outbox.messages == [] and client.requests_used == 1


def test_few_shots_in_second_half_skip_the_exact_request(tmp_path, conn):
    seed(conn, hs=(3, 1))
    full = detail_item(1, possession="68%", away_possession="32%", shots_home=4, shots_away=1, minute=70, home=0, away=1)
    client, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}])
    assert outbox.messages == [] and client.requests_used == 2


def test_exact_below_threshold_sends_nothing(tmp_path, conn):
    seed(conn)
    full = detail_item(1, possession="68%", away_possession="32%", shots_home=4, shots_away=1, minute=70, home=0, away=1)
    _, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}, exact_body(70, 3)])
    assert outbox.messages == []
