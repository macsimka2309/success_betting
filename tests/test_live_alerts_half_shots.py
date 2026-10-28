"""Категория half_shots, минуты 60–75: по таймам владение >= 70 % и 2+ удара, иначе за матч владение >= 70 % и 3+ удара (specs/онлайн-оповещения.md)."""

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


@pytest.mark.parametrize("poss,shots,expected", [(71, 2, True), (70, 2, True), (69, 5, False), (80, 1, False), (100, 9, True)])
def test_halves_possession_70_or_more_and_two_shots(poss, shots, expected):
    assert (la.build_half_shots_alert(item(), ex(poss, shots), {}) is not None) == expected


def test_either_team_and_message():
    alert = la.build_half_shots_alert(item(), ex(25, 0, 75, 4), {"home": 40, "away": 60})
    assert alert.team_side == "away" and alert.shots == 4 and alert.opponent_shots == 0
    text = la.format_message(alert)
    assert "Тип: владение и удары во 2-м тайме" in text and "владение 75 %, удары в створ 4" in text


@pytest.mark.parametrize("goals", [(0, 1), (3, 1), (0, 4)])
def test_score_is_not_a_condition(goals):
    assert la.build_half_shots_alert(item(home=goals[0], away=goals[1]), ex(75, 2), {}) is not None


@pytest.mark.parametrize("minute,expected", [(59, False), (60, True), (75, True), (76, False)])
def test_minute_window(minute, expected):
    assert (la.build_half_shots_alert(item(minute=minute), ex(75, 4), {}) is not None) == expected


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
    full = detail_item(1, possession="68%", away_possession="32%", shots_home=5, shots_away=1, minute=70, home=0, away=1)
    client, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}, exact_body(78, 4)])
    assert len(outbox.messages) == 1 and "владение и удары во 2-м тайме" in outbox.messages[0]
    assert client.requests_used == 3


def test_not_repeated_for_the_same_match(tmp_path, conn):
    seed(conn)
    full = detail_item(1, possession="68%", away_possession="32%", shots_home=5, shots_away=1, minute=70, home=0, away=1)
    run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}, exact_body(78, 4)])
    client, outbox = run(tmp_path / "b", conn, [{"response": [item()]}])
    assert outbox.messages == [] and client.requests_used == 1


def test_few_shots_in_second_half_skip_the_exact_request(tmp_path, conn):
    seed(conn, hs=(4, 1))  # во втором тайме 5-4=1 удар
    full = detail_item(1, possession="68%", away_possession="32%", shots_home=5, shots_away=1, minute=70, home=0, away=1)
    client, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}])
    assert outbox.messages == [] and client.requests_used == 2


def test_exact_below_threshold_sends_nothing(tmp_path, conn):
    seed(conn)
    full = detail_item(1, possession="68%", away_possession="32%", shots_home=5, shots_away=1, minute=70, home=0, away=1)
    _, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}, exact_body(69, 4)])
    assert outbox.messages == []


# ----------------------------------------------- запасной вариант: статистика за матч


def match_item(poss="75%", shots=4, home=0, away=1, minute=70):
    return detail_item(1, possession=poss, away_possession=f"{100 - int(poss[:-1])}%", shots_home=shots, shots_away=0,
                       minute=minute, home=home, away=away)


def test_match_fallback_without_halftime_snapshot(tmp_path, conn):
    client, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [match_item()]}])
    assert len(outbox.messages) == 1
    assert "За матч (по таймам данных нет): владение 75 %, удары в створ 4" in outbox.messages[0]
    assert client.requests_used == 2


@pytest.mark.parametrize("poss,shots", [("69%", 5), ("80%", 2)])
def test_match_fallback_uses_the_same_thresholds(tmp_path, conn, poss, shots):
    _, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [match_item(poss, shots)]}])
    assert outbox.messages == []


def test_match_fallback_window_and_score(tmp_path, conn):
    assert la.build_match_shots_alert(match_item(home=3, away=1)) is not None  # счёт не фильтруется
    assert la.build_match_shots_alert(match_item(minute=59)) is None
    assert la.build_match_shots_alert(match_item(minute=60)) is not None


def test_match_fallback_when_halves_endpoint_returns_nothing(tmp_path, conn):
    seed(conn)
    full = match_item("72%", 5)
    _, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}, {"response": []}])
    assert len(outbox.messages) == 1 and "За матч" in outbox.messages[0]


def test_match_fallback_not_used_when_halves_say_no(tmp_path, conn):
    seed(conn)
    full = match_item("72%", 5)
    _, outbox = run(tmp_path, conn, [{"response": [item()]}, {"response": [full]}, exact_body(65, 4)])
    assert outbox.messages == []


def test_match_fallback_not_repeated(tmp_path, conn):
    run(tmp_path, conn, [{"response": [item()]}, {"response": [match_item()]}])
    client, outbox = run(tmp_path / "b", conn, [{"response": [item()]}])
    assert outbox.messages == [] and client.requests_used == 1
