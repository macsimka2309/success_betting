"""Тесты категории half_attack: во втором тайме владение больше 55 % и 4+ ударов в створ."""

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


def item_at(fid, poss_home, poss_away, shots_home, shots_away, minute=65):
    return detail_item(fid, possession=f"{poss_home}%", away_possession=f"{poss_away}%", minute=minute,
                       shots_home=shots_home, shots_away=shots_away, home=0, away=0)


def exact_body(hp, hs, ap, as_):
    def block(p, s):
        return [{"type": "Ball Possession", "value": f"{p}%"}, {"type": "Shots on Goal", "value": s}]

    return {"response": [
        {"team": {"id": 10}, "statistics": block(50, 0), "statistics_2h": block(hp, hs)},
        {"team": {"id": 20}, "statistics": block(50, 0), "statistics_2h": block(ap, as_)},
    ]}


def stats(possession, shots):
    return {"possession": possession, "shots": shots}


def seed_snapshot(conn, fid, hp=50, ap=50, hs=2, as_=1):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO live_ht_possession (fixture_id, home_possession, away_possession, home_shots_on_goal, "
            "away_shots_on_goal) VALUES (%s, %s, %s, %s, %s)", (fid, hp, ap, hs, as_)
        )


def run(tmp_path, conn, bodies, **kwargs):
    client = make_client(tmp_path, bodies, max_requests=kwargs.pop("max_requests", 30))
    outbox = Outbox()
    ctx = la.run_live_alerts(client, conn, outbox, log=lambda _: None, **kwargs)
    return client, outbox, ctx


# ------------------------------------------------------------- триггер


@pytest.mark.parametrize(
    "possession,shots,expected",
    [(56, 4, True), (55, 4, False), (56, 3, False), (90, 3, False), (40, 9, False), (100, 4, True)],
)
def test_attack_boundaries(possession, shots, expected):
    item = item_at(1, 50, 50, 1, 1)
    alert = la.build_attack_alert(item, {10: stats(possession, shots), 20: stats(100 - possession, 0)}, {"home": 50, "away": 50})
    assert (alert is not None) == expected


def test_attack_either_team_and_message():
    item = item_at(1, 40, 60, 0, 6)
    alert = la.build_attack_alert(item, {10: stats(30, 1), 20: stats(70, 5)}, {"home": 40, "away": 60})

    assert alert.team_side == "away" and (alert.possession, alert.shots, alert.opponent_shots) == (70, 5, 1)
    text = la.format_message(alert)
    assert "Тип: атака во 2-м тайме" in text and "Команда: Away FC (гости)" in text
    assert "Во 2-м тайме: владение 70 %, удары в створ 5 (у соперника 1)" in text


def test_attack_missing_values_do_not_alert():
    item = item_at(1, 50, 50, 1, 1)
    assert la.build_attack_alert(item, {10: stats(None, 5), 20: stats(60, None)}, {}) is None
    assert la.build_attack_alert(item, {}, {}) is None


def test_attack_only_in_window():
    exact = {10: stats(60, 5), 20: stats(40, 0)}
    assert la.build_attack_alert(item_at(1, 50, 50, 1, 1, minute=59), exact, {}) is None
    assert la.build_attack_alert(item_at(1, 50, 50, 1, 1, minute=76), exact, {}) is None


# ------------------------------------------------------------- снимок с ударами


def test_half_time_snapshot_keeps_shots(tmp_path, conn):
    ht = detail_item(1, possession="55%", away_possession="45%", shots_home=3, shots_away=1, minute=45, status="HT")
    run(tmp_path, conn, [{"response": [live_item(1, minute=45, home=0, away=0, status="HT")]}, {"response": [ht]}])

    with conn.cursor() as cur:
        cur.execute("SELECT home_possession, away_possession, home_shots_on_goal, away_shots_on_goal FROM live_ht_possession")
        assert cur.fetchall() == [(55, 45, 3, 1)]


# ------------------------------------------------------------- полный цикл


def test_attack_alert_fires(tmp_path, conn):
    seed_snapshot(conn, 1, hs=1)  # на перерыве 1 удар; сейчас 5 => во втором тайме 4
    bodies = [{"response": [live_item(1, home=0, away=0)]}, {"response": [item_at(1, 58, 42, 5, 1)]}, exact_body(61, 4, 39, 0)]
    client, outbox, _ = run(tmp_path, conn, bodies)

    assert client.requests_used == 3 and "half=true" in client.transport.calls[2]
    assert len(outbox.messages) == 1 and "Тип: атака во 2-м тайме" in outbox.messages[0]
    with conn.cursor() as cur:
        cur.execute("SELECT category, possession, shots_on_goal, opponent_shots_on_goal FROM live_alerts_sent")
        assert cur.fetchall() == [("half_attack", 61, 4, 0)]


def test_not_enough_second_half_shots_costs_no_exact_request(tmp_path, conn):
    seed_snapshot(conn, 1, hs=2)  # 5 - 2 = 3 удара во втором тайме
    client, outbox, _ = run(tmp_path, conn, [{"response": [live_item(1, home=0, away=0)]}, {"response": [item_at(1, 53, 47, 5, 1)]}])

    assert client.requests_used == 2 and outbox.messages == []


def test_exact_values_decide(tmp_path, conn):
    seed_snapshot(conn, 1, hs=1)
    bodies = [{"response": [live_item(1, home=0, away=0)]}, {"response": [item_at(1, 58, 42, 5, 1)]}, exact_body(54, 4, 46, 0)]
    _, outbox, ctx = run(tmp_path, conn, bodies)

    assert outbox.messages == [] and ctx.skipped == 1


def test_snapshot_without_shots_skips_attack_check(tmp_path, conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO live_ht_possession (fixture_id, home_possession, away_possession) VALUES (1, 50, 50)")
    client, outbox, _ = run(tmp_path, conn, [{"response": [live_item(1, home=0, away=0)]}, {"response": [item_at(1, 53, 47, 5, 1)]}])

    assert client.requests_used == 2 and outbox.messages == []


def test_possession_prefilter_saves_request(tmp_path, conn):
    """Удары есть, но оценка владения во втором тайме у обеих команд низкая."""
    seed_snapshot(conn, 1, hp=58, ap=42, hs=1, as_=1)  # итог 52/48 при первом тайме 58/42 => 2-й тайм ≈ 38/61
    item = item_at(1, 52, 48, 5, 0)
    client, outbox, _ = run(tmp_path, conn, [{"response": [live_item(1, home=0, away=0)]}, {"response": [item]}])

    assert client.requests_used == 2 and outbox.messages == []


def test_one_exact_request_serves_both_second_half_categories(tmp_path, conn):
    seed_snapshot(conn, 1, hs=1)
    bodies = [{"response": [live_item(1, home=0, away=0)]}, {"response": [item_at(1, 62, 38, 5, 1)]}, exact_body(75, 4, 25, 0)]
    client, outbox, _ = run(tmp_path, conn, bodies)

    assert client.requests_used == 3  # live, ids, один точный запрос на обе категории
    titles = sorted(m.splitlines()[1] for m in outbox.messages)
    assert titles == ["Тип: атака во 2-м тайме", "Тип: владение во 2-м тайме"]


def test_attack_alert_not_repeated(tmp_path, conn):
    seed_snapshot(conn, 1, hs=1)
    bodies = [{"response": [live_item(1, home=0, away=0)]}, {"response": [item_at(1, 58, 42, 5, 1)]}, exact_body(61, 4, 39, 0)]
    run(tmp_path, conn, bodies)

    client, outbox, _ = run(tmp_path / "b", conn, [{"response": [live_item(1, home=0, away=0)]}, {"response": [item_at(1, 58, 42, 5, 1)]}, exact_body(75, 5, 25, 0)])

    # stats-категория по матчу ещё не сработала, ids= запрашивается; half сработал бы на 75 %,
    # а half_attack уже отправлена и повторно не уходит
    assert [m.splitlines()[1] for m in outbox.messages] == ["Тип: владение во 2-м тайме"]
    assert client.requests_used == 3
