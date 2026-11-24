"""Regression tests: only scripted API and a disposable TEST_DATABASE_URL."""
from datetime import date, datetime, timezone

import pytest

from src.jobs import collect
from tests.test_collect import conn, make_client  # noqa: F401
from tests.test_backfill import seed
from tests.test_statistics_halves import full_body

from tests.test_collect import pytestmark  # noqa: F401


def result(conn, kind):
    return conn.execute(
        'SELECT status, attempts FROM fixture_fetch_results WHERE fixture_id=1 AND kind=%s', (kind,)
    ).fetchone()


def due(conn):
    conn.execute("UPDATE fixture_fetch_results SET next_retry_at=now()-interval '1 second'")


def test_empty_is_delayed_and_does_not_poison_next_response(tmp_path, conn):
    seed(conn, [(1, datetime.now(timezone.utc).date())])
    client = make_client(tmp_path, [{'response': []}, full_body()])
    collect.run_statistics(client, conn)
    assert result(conn, 'statistics') == ('empty', 1)
    collect.run_statistics(client, conn)
    assert client.requests_used == 1
    due(conn)
    collect.run_statistics(client, conn)
    assert client.requests_used == 2
    assert result(conn, 'statistics') == ('loaded', 2)
    assert conn.execute('SELECT count(*) FROM fixture_statistics').fetchone()[0] == 2


def test_empty_exhausts_without_claiming_unsupported(tmp_path, conn):
    seed(conn, [(1, datetime.now(timezone.utc).date())])
    client = make_client(tmp_path, [{'response': []}] * 4)
    for _ in range(4):
        collect.run_statistics(client, conn)
        due(conn)
    assert result(conn, 'statistics') == ('exhausted', 4)
    collect.run_statistics(client, conn)
    assert client.requests_used == 4
    assert conn.execute('SELECT count(*) FROM fixture_statistics').fetchone()[0] == 0


def test_partial_team_stats_and_halves_are_not_complete(tmp_path, conn):
    seed(conn, [(1, datetime.now(timezone.utc).date())])
    body = full_body()
    body['response'] = body['response'][:1]
    collect._process_statistics(make_client(tmp_path, [body]), conn, 1)
    assert result(conn, 'statistics') == ('partial', 1)
    assert result(conn, 'halves') == ('partial', 1)


def test_archive_preserves_all_live_polls_and_excludes_cache(tmp_path, conn):
    client = make_client(tmp_path, [{'response': [1]}, {'response': [2]}])
    with collect.collection_run(conn, client, 'test'):
        client.get('/fixtures', {'live': 'all'}, use_cache=False)
        client.get('/fixtures', {'live': 'all'})
        client.get('/fixtures', {'live': 'all'}, use_cache=False)
    rows = conn.execute('SELECT payload, requested_at<=observed_at FROM api_observations ORDER BY observation_id').fetchall()
    assert rows == [({'response': [1]}, True), ({'response': [2]}, True)]
    assert client.observer is None


def test_archive_failure_aborts_collection(tmp_path, conn):
    client = make_client(tmp_path, [{'response': []}])
    conn.execute('ALTER TABLE api_observations RENAME TO observations_unavailable')
    with pytest.raises(Exception):
        collect.run_live_odds(client, conn)
    assert client.observer is None
    assert conn.execute("SELECT status FROM collection_runs").fetchone()[0] == 'failed'


@pytest.mark.parametrize('failed', [False, True])
def test_features_depend_on_fresh_and_precede_history(tmp_path, conn, monkeypatch, failed):
    client = make_client(tmp_path, [])
    order = []
    def fresh(*args, **kwargs):
        order.append('fresh')
        if failed:
            client.failed_requests += 1
        return collect.StepContext(1)
    for name in ('run_fixtures', 'run_injuries', 'run_events', 'run_statistics', 'run_lineups'):
        monkeypatch.setattr(collect, name, fresh)
    monkeypatch.setattr(collect, 'run_backfill', lambda *a, **kw: order.append('history'))
    if failed:
        with pytest.raises(RuntimeError):
            collect.run_daily(client, conn, after_fresh=lambda: order.append('features'))
        assert order == ['fresh'] * 5
    else:
        collect.run_daily(client, conn, after_fresh=lambda: order.append('features'))
        assert order == ['fresh'] * 5 + ['features', 'history']


def test_lineups_with_empty_starters_are_not_loaded(tmp_path, conn):
    seed(conn, [(1, datetime.now(timezone.utc).date())])
    body = {'response': [{'team': {'id': n}, 'startXI': []} for n in (36, 34)]}
    assert not collect._process_lineups(make_client(tmp_path, [body]), conn, 1)
    assert result(conn, 'lineups') == ('partial', 1)
    assert conn.execute('SELECT count(*) FROM fixture_fetch_state WHERE lineups_fetched_at IS NOT NULL').fetchone()[0] == 0


@pytest.mark.parametrize('budget, complete', [(1, False), (2, True)])
def test_live_odds_partial_pagination_is_not_success(tmp_path, conn, budget, complete):
    body = {'response': [1], 'paging': {'total': 2}}
    client = make_client(tmp_path, [body, {'response': [2]}], max_requests=budget)
    if complete:
        assert collect.run_live_odds(client, conn).items_processed == 2
    else:
        with pytest.raises(RuntimeError):
            collect.run_live_odds(client, conn)
    assert conn.execute('SELECT status FROM collection_runs').fetchone()[0] == ('quota_exceeded' if complete else 'failed')
    assert conn.execute('SELECT count(*) FROM api_observations').fetchone()[0] == budget


def test_feature_failure_prevents_history(tmp_path, conn, monkeypatch):
    client = make_client(tmp_path, [])
    def fresh(*args, **kwargs):
        return collect.StepContext(1)
    for name in ('run_fixtures', 'run_injuries', 'run_events', 'run_statistics', 'run_lineups'):
        monkeypatch.setattr(collect, name, fresh)
    monkeypatch.setattr(collect, 'run_backfill', lambda *a, **kw: pytest.fail('history started'))
    def fail():
        raise RuntimeError('features failed')
    with pytest.raises(RuntimeError, match='features failed'):
        collect.run_daily(client, conn, after_fresh=fail)


def test_new_migration_can_be_reverted_on_empty_test_database(conn):
    from src.db import migrate
    migration = next(m for m in migrate.discover() if m.version == '019')
    dependents = [m for m in migrate.discover() if m.version > '019']
    for dependent in reversed(dependents):
        migrate.revert(conn, dependent)
    migrate.revert(conn, migration)
    assert conn.execute("SELECT to_regclass('api_observations'),to_regclass('fixture_fetch_results')").fetchone() == (None, None)
    migrate.apply(conn, migration)
    for dependent in dependents:
        migrate.apply(conn, dependent)
    assert conn.execute("SELECT count(*) FROM api_observations").fetchone()[0] == 0
