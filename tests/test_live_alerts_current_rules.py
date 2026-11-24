"""Current production rules, without API, Telegram or database access."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.jobs import live_alerts as la
from tests.test_live_alerts import detail_item, live_item, Outbox
from tests.test_collect import make_client


@pytest.mark.parametrize("possession,shots,opponent,expected", [
    (70, 3, None, True), (70, 2, 2, True), (70, 1, 3, True),
    (70, 2, 1, False), (70, 2, None, False), (69, 3, 1, False),
])
def test_first_signal_thresholds(possession, shots, opponent, expected):
    exact = {10: {"possession": possession, "shots": shots}, 20: {"shots": opponent}}
    assert (la.build_half_shots_alert(live_item(1), exact, {}) is not None) == expected


def test_second_half_rule_at_75(tmp_path, monkeypatch):
    saved = []
    @contextmanager
    def collection(*args):
        yield SimpleNamespace(run_id=1, items_processed=0, skipped=0)
    monkeypatch.setattr(la, 'collection_run', collection)
    monkeypatch.setattr(la, '_already_sent', lambda *a: set())
    monkeypatch.setattr(la, '_ht_snapshots', lambda conn, ids: {1: {
        'possession': {'home': 50, 'away': 50}, 'shots': {'home': 1, 'away': 0}
    }} if 1 in ids else {})
    monkeypatch.setattr(la, '_purge_old_snapshots', lambda *a: None)
    monkeypatch.setattr(la, '_reserve_alert_id', lambda *a: 1)
    monkeypatch.setattr(la, '_record_sent', lambda conn, alert: saved.append(alert))
    client = make_client(tmp_path, [
        {'response': [live_item(1, minute=75)]},
        {'response': [{'team': {'id': 10}, 'statistics_2h': [
            {'type': 'Ball Possession', 'value': '70%'}, {'type': 'Shots on Goal', 'value': 3}
        ]}]},
    ])
    la.run_live_alerts(client, None, Outbox(), categories=("half_shots",))
    assert len(saved) == 1 and saved[0].source == 'half'
    assert saved[0].shots == 3 and client.requests_used == 2


def test_only_over_buttons_with_three_lines():
    from dataclasses import replace
    alert = la.build_half_shots_alert(live_item(1), {10: {'possession': 75, 'shots': 3}}, {})
    alert = replace(alert, alert_id=123, goals_home=2, goals_away=1)
    buttons = [b for row in la.bet_keyboard(alert)['inline_keyboard'] for b in row]
    assert [b['text'] for b in buttons] == ['ТБ 3.5', 'ТБ 4.5', 'ТБ 5.5']
    assert [b['callback_data'] for b in buttons] == ['bet:123:TB:3.5', 'bet:123:TB:4.5', 'bet:123:TB:5.5']
    from src.bot.registry_bot import MARKET_KEYBOARD
    assert all(b['text'] != 'ТМ' for row in MARKET_KEYBOARD['inline_keyboard'] for b in row)


@pytest.mark.parametrize('home,away,hit', [(3,3,False),(2,4,False),(3,4,False),(4,3,False),(4,4,True),(5,0,False),(0,5,False),(6,0,True),(0,6,True),(3,2,False),(4,0,False),(4,None,False),(5,None,False),(6,None,True),(None,None,False)])
def test_pressure_thresholds(home,away,hit):
    exact={10:{'shots':home},20:{'shots':away}}
    alert=la.build_pressure_alert(live_item(1,minute=70),exact)
    assert (alert is not None)==hit
    if alert:
        assert 'None %' not in la.format_message(alert)
        assert alert.possession is None


@pytest.mark.parametrize('minute,draw,hit',[(64,False,False),(65,False,True),(80,False,False),(81,False,False),(70,True,True)])
def test_pressure_window(minute,draw,hit):
    assert (la.build_pressure_alert(live_item(1,minute=minute,home=0,away=0 if draw else 1),{10:{'shots':6}}) is not None)==hit


def test_legacy_pressure_ignores_full_match_statistics():
    body = {'response': [{'team': {'id': 10}, 'statistics': [{'type': 'Shots on Goal', 'value': 9}]}]}
    assert la.build_pressure_alert(live_item(1, minute=70), la._half_stats_exact(body)) is None


@pytest.mark.parametrize("away", [0, 1])
def test_pressure_no_snapshot_and_no_repeat(tmp_path,monkeypatch,away):
    saved=[]
    @contextmanager
    def collection(*args):
        yield SimpleNamespace(run_id=1, items_processed=0,skipped=0)
    monkeypatch.setattr(la,'collection_run',collection)
    monkeypatch.setattr(la,'_already_sent',lambda conn,ids,category: {a.fixture_id for a in saved if a.category==category})
    monkeypatch.setattr(la,'_reserve_alert_id',lambda *a: 1)
    monkeypatch.setattr(la,'_record_sent',lambda conn,a:saved.append(a))
    body={'response':[{'team':{'id':10},'statistics_2h':[{'type':'Shots on Goal','value':3}]}, {'team':{'id':20},'statistics_2h':[{'type':'Shots on Goal','value':5}]}]}
    match = live_item(1,minute=75,home=0,away=away)
    match['events'] = [{'type': 'Card', 'detail': 'Yellow Card'}] * 7
    assert la.ENABLED_CATEGORIES == ('half_pressure',)
    client=make_client(tmp_path,[{'response':[match]},body])
    outbox=Outbox()
    la.run_live_alerts(client,None,outbox)
    assert len(saved)==1 and len(outbox.messages)==1
    assert 'суммарно 8' in outbox.messages[0]
    client=make_client(tmp_path/'again',[{'response':[live_item(1,minute=75,home=0,away=away)]}])
    la.run_live_alerts(client,None,outbox)
    assert len(outbox.messages)==1 and client.requests_used==1


@pytest.mark.parametrize("yellow,expected", [(6, 0), (7, 1), (8, 1)])
def test_yellow_card_signal(tmp_path, monkeypatch, yellow, expected):
    saved = []

    @contextmanager
    def collection(*args):
        yield SimpleNamespace(run_id=1, items_processed=0, skipped=0)

    monkeypatch.setattr(la, "collection_run", collection)
    monkeypatch.setattr(la, "_already_sent", lambda conn, ids, category: {
        alert.fixture_id for alert in saved if alert.category == category
    })
    monkeypatch.setattr(la, "_reserve_alert_id", lambda conn: 1)
    monkeypatch.setattr(la, "_record_sent", lambda conn, alert: saved.append(alert))
    match = live_item(1, minute=70, home=1, away=1)
    match["events"] = ([{"type": "Card", "detail": "Yellow Card"}] * yellow
                       + [{"type": "Card", "detail": "Red Card"}])
    outbox = Outbox()
    for path in (tmp_path / "first", tmp_path / "again"):
        client = make_client(path, [{"response": [match]}])
        la.run_live_alerts(client, None, outbox, categories=("yellow_cards",))
        assert client.requests_used == 1
    assert len(saved) == len(outbox.messages) == expected
    if expected:
        assert f"Жёлтые карточки за матч: {yellow}" in outbox.messages[0]
        assert outbox.markups == [None]


@pytest.fixture(autouse=True)
def isolate_alert_rules(monkeypatch):
    """Persistence and its requests are exercised with a real DB in test_live_statistics."""
    monkeypatch.setattr(la, "collect_statistics", lambda *args: {})
