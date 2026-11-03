from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from src.jobs import collect


@pytest.mark.parametrize('empty', [False, True])
def test_history_queue_progress_and_budget(monkeypatch, empty):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [(17, 2025), (18, 2024)]
    client = SimpleNamespace(requests_used=0, max_requests=1, quota_exhausted=False)
    journals = []
    @contextmanager
    def journal(conn, client, name):
        ctx = SimpleNamespace(items_processed=0)
        try:
            yield ctx
        except RuntimeError:
            journals.append((name, 'failed'))
            raise
        else:
            journals.append((name, 'success'))
    def get(*args, **kwargs):
        assert kwargs['use_cache'] is False
        client.requests_used += 1
        return {'response': [] if empty else [{}]}
    client.get = get
    monkeypatch.setattr(collect, 'collection_run', journal)
    parsed = {c: 1 for c in collect.FIXTURE_COLUMNS}
    parsed.update(home_team_name='A', away_team_name='B')
    monkeypatch.setattr(collect, '_parse_fixture_item', lambda *a: parsed)
    monkeypatch.setattr(collect, '_upsert_team', MagicMock())
    writer = MagicMock()
    monkeypatch.setattr(collect, 'write_row', writer)
    collect.run_historical_fixtures(client, conn)
    assert journals == [('historical_fixtures_17_2025', 'failed' if empty else 'success')]
    assert writer.call_count == (0 if empty else 1)
    sql, params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args
    assert "r.status = 'success'" in sql
    assert 'ls.season >= 2018' in sql
    assert len(params[0]) == 19


def test_completed_queue_spends_no_requests():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    client = SimpleNamespace(get=MagicMock())
    collect.run_historical_fixtures(client, conn)
    client.get.assert_not_called()
