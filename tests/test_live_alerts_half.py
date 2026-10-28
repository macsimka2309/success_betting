"""Тесты категории half: владение во втором тайме больше 70 % (specs/онлайн-оповещения.md)."""

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


def ex(possession_by_team):
    """Точные показатели второго тайма в виде, который возвращает парсер."""
    return {team: {"possession": value, "shots": None} for team, value in possession_by_team.items()}


def exact_body(home_2h, away_2h):
    def block(value):
        return [{"type": "Ball Possession", "value": value}]

    return {"response": [
        {"team": {"id": 10}, "statistics": block("60%"), "statistics_1h": block("50%"), "statistics_2h": block(home_2h)},
        {"team": {"id": 20}, "statistics": block("40%"), "statistics_1h": block("50%"), "statistics_2h": block(away_2h)},
    ]}


def ht_item(fid, home=50, away=50):
    item = detail_item(fid, possession=f"{home}%", away_possession=f"{away}%", minute=45, status="HT", shots_home=1, shots_away=1)
    return item


def total_item(fid, home_total, away_total, minute=65):
    return detail_item(fid, possession=f"{home_total}%", away_possession=f"{away_total}%", minute=minute,
                       shots_home=1, shots_away=1, home=0, away=0)


def seed_snapshot(conn, fid, home, away):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO live_ht_possession (fixture_id, home_possession, away_possession) VALUES (%s, %s, %s)",
                    (fid, home, away))


def run(tmp_path, conn, bodies, outbox=None, **kwargs):
    client = make_client(tmp_path, bodies, max_requests=kwargs.pop("max_requests", 30))
    outbox = outbox if outbox is not None else Outbox()
    ctx = la.run_live_alerts(client, conn, outbox, log=lambda _: None, **kwargs)
    return client, outbox, ctx


# ------------------------------------------------------------- оценка (чистые функции)


def test_estimate_second_half():
    # на 65-й минуте: итог 58 %, первый тайм 50 % => (58*65 - 50*45)/20 = 76
    assert la.estimate_second_half(58, 50, 65) == pytest.approx(76.0)
    assert la.estimate_second_half(50, 50, 65) == pytest.approx(50.0)


def test_estimate_not_available_in_first_half():
    assert la.estimate_second_half(60, 50, 45) is None
    assert la.estimate_second_half(60, 50, 30) is None


@pytest.mark.parametrize("value,expected", [(70, True), (71, True), (69, False), (100, True)])
def test_exact_threshold_is_70_or_more(value, expected):
    item = total_item(1, 60, 40)
    alert = la.build_half_alert(item, ex({10: value, 20: 100 - value}), {"home": 60, "away": 40})
    assert (alert is not None and alert.team_side == "home") == expected


def test_either_team_can_trigger():
    alert = la.build_half_alert(total_item(1, 40, 60), ex({10: 28, 20: 72}), {"home": 40, "away": 60})
    assert alert.team_side == "away" and alert.possession == 72 and alert.total_possession == 60


def test_half_alert_ignores_outside_window_and_status():
    assert la.build_half_alert(total_item(1, 60, 40, minute=64), ex({10: 80, 20: 20}), {}) is None
    assert la.build_half_alert(total_item(1, 60, 40, minute=76), ex({10: 80, 20: 20}), {}) is None
    item = total_item(1, 60, 40)
    item["fixture"]["status"]["short"] = "HT"
    assert la.build_half_alert(item, ex({10: 80, 20: 20}), {}) is None


def test_exact_parser_handles_missing_blocks():
    assert la._half_stats_exact({"response": [{"team": {"id": 10}, "statistics": []}]}) == {10: {"possession": None, "shots": None}}
    assert la._half_stats_exact(None) == {}
    assert la._half_stats_exact(exact_body("74%", "26%"))[10] == {"possession": 74, "shots": None}


def test_message_for_half_category():
    alert = la.build_half_alert(total_item(1, 60, 40), ex({10: 74, 20: 26}), {"home": 60, "away": 40})
    text = la.format_message(alert)
    assert "Тип: владение во 2-м тайме" in text and "Владение во 2-м тайме: 74 % (за матч 60 %)" in text
    assert "Команда: Home FC (хозяева)" in text and "Удары" not in text


# ------------------------------------------------------------- снимок на перерыве


def test_half_time_snapshot_is_taken_once(tmp_path, conn):
    run(tmp_path, conn, [{"response": [live_item(1, minute=45, home=0, away=0, status="HT")]}, {"response": [ht_item(1, 55, 45)]}])
    with conn.cursor() as cur:
        cur.execute("SELECT home_possession, away_possession FROM live_ht_possession WHERE fixture_id = 1")
        assert cur.fetchone() == (55, 45)

    client, _, _ = run(tmp_path / "b", conn, [{"response": [live_item(1, minute=45, home=0, away=0, status="HT")]}])
    assert client.requests_used == 1  # снимок уже есть, статистику на перерыве не запрашиваем


def test_snapshots_are_batched_and_missing_stats_not_saved(tmp_path, conn):
    no_stats = live_item(2, minute=45, home=0, away=0, status="HT")
    bodies = [{"response": [live_item(1, minute=45, home=0, away=0, status="HT"), live_item(2, minute=45, home=0, away=0, status="HT")]},
              {"response": [ht_item(1), no_stats]}]
    client, _, _ = run(tmp_path, conn, bodies)

    assert client.requests_used == 2
    with conn.cursor() as cur:
        cur.execute("SELECT fixture_id FROM live_ht_possession")
        assert cur.fetchall() == [(1,)]


def test_old_snapshots_are_purged(tmp_path, conn):
    seed_snapshot(conn, 99, 50, 50)
    with conn.cursor() as cur:
        cur.execute("UPDATE live_ht_possession SET captured_at = now() - interval '3 days'")
    run(tmp_path, conn, [{"response": []}])
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM live_ht_possession")
        assert cur.fetchone()[0] == 0


# ------------------------------------------------------------- полный цикл


def test_half_alert_fires_after_prefilter_and_exact_check(tmp_path, conn):
    seed_snapshot(conn, 1, 50, 50)
    bodies = [{"response": [live_item(1, home=0, away=0)]},
              {"response": [total_item(1, 60, 40)]},   # оценка ≈ 77 > 62 => точный запрос
              exact_body("74%", "26%")]
    client, outbox, _ = run(tmp_path, conn, bodies)

    assert client.requests_used == 3
    assert "half=true" in client.transport.calls[2] and "fixtures/statistics" in client.transport.calls[2]
    assert len(outbox.messages) == 1 and "Тип: владение во 2-м тайме" in outbox.messages[0]
    with conn.cursor() as cur:
        cur.execute("SELECT category, possession, total_possession, shots_on_goal FROM live_alerts_sent")
        assert cur.fetchall() == [("half", 74, 60, None)]


def test_exact_check_below_70_sends_nothing(tmp_path, conn):
    seed_snapshot(conn, 1, 50, 50)
    bodies = [{"response": [live_item(1, home=0, away=0)]}, {"response": [total_item(1, 60, 40)]}, exact_body("68%", "32%")]
    _, outbox, ctx = run(tmp_path, conn, bodies)

    assert outbox.messages == [] and ctx.skipped == 1


def test_prefilter_saves_the_exact_request(tmp_path, conn):
    seed_snapshot(conn, 1, 50, 50)  # итог 52/48 при первом тайме 50/50 => второй тайм ≈ 56: не кандидат
    client, outbox, _ = run(tmp_path, conn, [{"response": [live_item(1, home=0, away=0)]}, {"response": [total_item(1, 52, 48)]}])

    assert client.requests_used == 2 and outbox.messages == []


def test_no_snapshot_means_no_half_check(tmp_path, conn):
    client, outbox, _ = run(tmp_path, conn, [{"response": [live_item(1, home=0, away=0)]}, {"response": [total_item(1, 80, 20)]}])

    assert client.requests_used == 2 and outbox.messages == []


def test_half_alert_not_repeated(tmp_path, conn):
    seed_snapshot(conn, 1, 50, 50)
    bodies = [{"response": [live_item(1, home=0, away=0)]}, {"response": [total_item(1, 60, 40)]}, exact_body("74%", "26%")]
    run(tmp_path, conn, bodies)

    # категория stats по матчу ещё не сработала, поэтому статистику запрашиваем снова,
    # но точный запрос по таймам уже не нужен: half по этому матчу отправлена
    client, outbox, _ = run(tmp_path / "b", conn,
                            [{"response": [live_item(1, home=0, away=0)]}, {"response": [total_item(1, 60, 40)]}])

    assert outbox.messages == [] and client.requests_used == 2


def test_both_categories_fire_on_one_match_in_one_card(tmp_path, conn):
    seed_snapshot(conn, 1, 50, 50)
    item = detail_item(1, possession="62%", away_possession="38%", shots_home=7, shots_away=1, home=0, away=0)
    bodies = [{"response": [live_item(1, home=0, away=0)]}, {"response": [item]}, exact_body("75%", "25%")]
    _, outbox, _ = run(tmp_path, conn, bodies)

    assert len(outbox.messages) == 1
    text = outbox.messages[0]
    assert "Признаки (2):" in text and "• владение и удары в створ" in text and "• владение во 2-м тайме" in text
    with conn.cursor() as cur:
        cur.execute("SELECT category, also_categories FROM live_alerts_sent")
        assert cur.fetchall() == [("stats", ["half"])]


def test_exact_request_respects_budget(tmp_path, conn):
    seed_snapshot(conn, 1, 50, 50)
    bodies = [{"response": [live_item(1, home=0, away=0)]}, {"response": [total_item(1, 60, 40)]}, exact_body("74%", "26%")]
    client, outbox, _ = run(tmp_path, conn, bodies, max_requests=2)

    assert client.requests_used == 2 and outbox.messages == []


def test_dry_run_half_does_not_record(tmp_path, conn):
    seed_snapshot(conn, 1, 50, 50)
    logged = []
    bodies = [{"response": [live_item(1, home=0, away=0)]}, {"response": [total_item(1, 60, 40)]}, exact_body("74%", "26%")]
    client = make_client(tmp_path, bodies)
    la.run_live_alerts(client, conn, None, dry_run=True, log=logged.append)

    assert len(logged) == 1 and "(half)" in logged[0]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM live_alerts_sent")
        assert cur.fetchone()[0] == 0


@pytest.fixture(autouse=True)
def _legacy_categories_enabled(monkeypatch):
    """Категории stats/half/half_attack отключены в продакшене (18.10.2026), здесь проверяется их логика."""
    monkeypatch.setattr(la, "ENABLED_CATEGORIES", ("stats", "half", "half_attack"))


@pytest.mark.parametrize("goals,expected", [((0, 1), True), ((1, 1), True), ((2, 1), True), ((3, 1), False), ((2, 0), False)])
def test_leader_by_more_than_one_goal_is_excluded(goals, expected):
    item = total_item(1, 60, 40)
    item["goals"] = {"home": goals[0], "away": goals[1]}
    alert = la.build_half_alert(item, ex({10: 75, 20: 25}), {})
    assert (alert is not None) == expected


def test_minute_window_65_to_75_for_half():
    for minute, expected in ((64, False), (65, True), (75, True)):
        alert = la.build_half_alert(total_item(1, 60, 40, minute=minute), ex({10: 75, 20: 25}), {})
        assert (alert is not None) == expected
