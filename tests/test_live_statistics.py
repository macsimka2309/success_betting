"""Live-statistics integration with scripted API and disposable PostgreSQL."""
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
import psycopg

from src.api.client import ObservationReceipt
from src.jobs import live_alerts as la
from src.jobs import live_statistics as stats
from tests.test_collect import conn, make_client  # noqa: F401
from tests.test_live_alerts import live_item

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='temporary test DB required')


def body(home=0, away=2):
    rows = []
    for team_id, shots in ((10, home), (20, away)):
        if shots is not None:
            rows.append({'team': {'id': team_id}, 'statistics_2h': [{'type': 'Shots on Goal', 'value': shots}]})
    return {'response': rows}


def receipts(conn):
    run_id = conn.execute("INSERT INTO collection_runs(job_name,status) VALUES ('test','success') RETURNING run_id").fetchone()[0]
    obs_id = conn.execute("INSERT INTO api_observations(run_id,endpoint,params,requested_at,observed_at,payload) VALUES (%s,'test','{}',now(),now(),'{}') RETURNING observation_id", (run_id,)).fetchone()[0]
    return ObservationReceipt(100, 101, obs_id), run_id


def row(conn):
    return conn.execute('SELECT elapsed, statistics, quality_status FROM live_match_statistics_75 WHERE fixture_id=1').fetchone()


@pytest.mark.parametrize('minute,eligible', [(64, False), (65, True), (75, True), (76, False)])
def test_boundaries(minute, eligible):
    assert stats.is_statistics_candidate(live_item(1, minute=minute)) == eligible


def test_upsert_preserves_best_minute_and_replaces_entire_payload(conn):
    receipt, run_id = receipts(conn)
    assert stats.save_statistics(conn, live_item(1, minute=70), body(), receipt, receipt, run_id) == 'inserted'
    assert stats.save_statistics(conn, live_item(1, minute=75), body(home=5, away=None), receipt, receipt, run_id) == 'updated'
    assert row(conn)[0] == 75
    assert row(conn)[2] == 'partial'
    assert row(conn)[1]['away']['2h'] is None
    assert stats.save_statistics(conn, live_item(1, minute=71), body(), replace(receipt, observed_at=102), receipt, run_id) == 'older_or_duplicate'
    assert stats.save_statistics(conn, live_item(1, minute=75), {'response': []}, receipt, receipt, run_id) == 'empty'
    assert row(conn)[0] == 75
    assert conn.execute('SELECT count(*) FROM live_match_statistics_75').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM fixtures').fetchone()[0] == 0  # external fixture ID is sufficient


def test_equal_minute_timestamps_and_idempotence(conn):
    receipt, run_id = receipts(conn)
    match = live_item(1, minute=75)
    stats.save_statistics(conn, match, body(), receipt, receipt, run_id)
    assert stats.save_statistics(conn, match, body(home=9), receipt, receipt, run_id) == 'older_or_duplicate'
    later = replace(receipt, observed_at=102)
    assert stats.save_statistics(conn, match, body(home=9), later, receipt, run_id) == 'updated'
    assert row(conn)[1]['home']['2h']['shots_on_goal'] == 9


def test_empty_zero_null_and_invalid_teams():
    item = live_item(1)
    assert stats.normalize_statistics(item, body(home=0, away=None))[2] == 'partial'
    assert stats.normalize_statistics(item, {'response': [{'team': {'id': 10}, 'statistics': [{'type': 'Total Shots', 'value': None}]}]}) is None
    bad = body()
    bad['response'][0]['team']['id'] = 999
    with pytest.raises(ValueError):
        stats.normalize_statistics(item, bad)


def test_stale_context_does_not_write(conn):
    receipt, run_id = receipts(conn)
    assert stats.save_statistics(conn, live_item(1), body(), replace(receipt, observed_at=132), receipt, run_id) == 'context_stale'
    assert row(conn) is None


def test_all_games_captured_despite_already_sent_and_reused_for_alert(tmp_path, conn, monkeypatch):
    monkeypatch.setattr(la, '_already_sent', lambda *args: {1})
    monkeypatch.setattr(la, '_record_sent', lambda *args: pytest.fail('repeat notification'))
    client = make_client(tmp_path, [{'response': [live_item(1, minute=75)]}, body(5, 0)])
    la.run_live_alerts(client, conn, None, dry_run=True, log=lambda _: None)
    assert row(conn)[0] == 75
    assert client.requests_used == 2
    # No signal/disabled categories still capture a later observation.
    client = make_client(tmp_path/'again', [{'response': [live_item(1, minute=75)]}, body(0, 0)])
    la.run_live_alerts(client, conn, None, dry_run=True, categories=(), log=lambda _: None)
    assert client.requests_used == 2
    assert row(conn)[1]['home']['2h']['shots_on_goal'] == 0


def test_budget_and_api_errors_logged(tmp_path, conn):
    client = make_client(tmp_path, [{'response': [live_item(1, minute=75)]}], max_requests=1)
    logs = []
    la.run_live_alerts(client, conn, None, dry_run=True, categories=(), log=logs.append)
    assert any('budget_exceeded' in line for line in logs)
    assert row(conn) is None
    client = make_client(tmp_path/'b', [{'response': [live_item(1, minute=75)]}, {'errors': {'server': 'failure'}}])
    la.run_live_alerts(client, conn, None, dry_run=True, categories=(), log=logs.append)
    assert any('api_error' in line for line in logs)
    assert row(conn) is None


def test_concurrent_writers_keep_latest_minute(conn):
    receipt, run_id = receipts(conn)
    def write(minute):
        with psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True) as other:
            stats.save_statistics(other, live_item(1, minute=minute), body(), receipt, receipt, run_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, (75, 70)))
    assert row(conn)[0] == 75


def test_telegram_failure_keeps_statistics(tmp_path, conn):
    # Reach the current >=6 team trigger so this tests persistence after send failure.
    client = make_client(tmp_path, [{'response': [live_item(1, minute=75)]}, body(6, 0)])
    def failed_send(*args):
        raise la.TelegramError('failure')
    with pytest.raises(RuntimeError, match='оповещений'):
        la.run_live_alerts(client, conn, failed_send, log=lambda _: None)
    assert row(conn)[0] == 75


def test_snapshot_migration_rollback_and_constraints(conn):
    from src.db import migrate
    migration = next(m for m in migrate.discover() if m.version == '023')
    dependents = [m for m in migrate.discover() if m.version > '023']
    for dependent in reversed(dependents):
        migrate.revert(conn, dependent)
    migrate.revert(conn, migration)
    assert conn.execute("SELECT to_regclass('live_match_statistics_75')").fetchone()[0] is None
    migrate.apply(conn, migration)
    for dependent in dependents:
        migrate.apply(conn, dependent)
    receipt, run_id = receipts(conn)
    stats.save_statistics(conn, live_item(1, minute=75), body(), receipt, receipt, run_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute('UPDATE live_match_statistics_75 SET elapsed=76')


def test_equal_time_observation_id_breaks_ties(conn):
    receipt, run_id = receipts(conn)
    stats.save_statistics(conn, live_item(1, minute=75), body(), receipt, receipt, run_id)
    second_id = conn.execute("INSERT INTO api_observations(run_id,endpoint,params,requested_at,observed_at,payload) VALUES (%s,'test','{}',now(),now(),'{}') RETURNING observation_id", (run_id,)).fetchone()[0]
    later = replace(receipt, observation_id=second_id)
    assert stats.save_statistics(conn, live_item(1, minute=75), body(home=7), later, receipt, run_id) == 'updated'


def test_nonmatching_fixture_id_is_rejected(conn):
    receipt, run_id = receipts(conn)
    wrong = body()
    wrong['parameters'] = {'fixture': 999}
    with pytest.raises(ValueError, match='unexpected_fixture'):
        stats.save_statistics(conn, live_item(1), wrong, receipt, receipt, run_id)
    assert row(conn) is None


def test_all_eligible_matches_even_without_alert_categories(tmp_path, conn):
    matches = [live_item(1, minute=65), live_item(2, minute=75), live_item(3, minute=76)]
    client = make_client(tmp_path, [{'response': matches}, body(0, None), body(0, 0)])
    la.run_live_alerts(client, conn, None, dry_run=True, categories=(), log=lambda _: None)
    assert conn.execute('SELECT fixture_id,elapsed FROM live_match_statistics_75 ORDER BY fixture_id').fetchall() == [(1, 65), (2, 75)]
    assert client.requests_used == 3


def test_missing_minute_is_logged_without_request():
    client = SimpleNamespace()
    logs = []
    responses = stats.collect_statistics(client, None, [live_item(1, minute=None)], None, 1, logs.append)
    assert responses == {}
    assert any("'invalid_context': 1" in line for line in logs)


@pytest.mark.parametrize('minute,stored', [(75, True), (76, False)])
def test_slow_poll_refreshes_context_before_statistics(tmp_path, conn, minute, stored):
    client = make_client(tmp_path, [
        {'response': [live_item(1, minute=70)]},
        {'response': [live_item(1, minute=minute)]},
        body(0, 0),
    ])
    live = client.get('/fixtures', {'live': 'all'}, use_cache=False)
    context = client.last_observation
    # Archive this direct test response and subsequent ones with real IDs.
    receipt, run_id = receipts(conn)
    context = replace(context, observation_id=receipt.observation_id)
    client.observer = lambda *args: receipt.observation_id
    client.sleep(21)
    logs = []
    stats.collect_statistics(client, conn, live['response'], context, run_id, logs.append)
    assert any('context_requests' in line for line in logs)
    assert (row(conn) is not None) == stored
    if stored:
        assert row(conn)[0] == 75
    assert client.requests_used == (3 if stored else 2)
