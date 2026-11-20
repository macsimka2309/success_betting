import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from src.jobs.alert_dispatch import rows
from src.jobs.signal_goal_store import relate_goals, save_timeline
from src.jobs.signal_goals import Timeline, api_timeline, load_fonbet_timeline
from src.jobs.signal_results import run_results
from src.jobs.signal_tracking import configure
from tests.test_alert_dispatch import NOW
from tests.test_collect import conn, make_client  # noqa: F401
from tests.test_live_source_matching import add_fonbet
from tests.test_signal_goals import fixture, goal
from tests.test_signal_lifecycle import first_signal
from tests.test_signal_review_regressions import signal

FIXTURES = Path('tests/fixtures/fonbet')


def test_partial_snapshot_cannot_resurrect_revoked_goal(conn):
    mid = signal(conn, 1)
    observations = [
        fixture([goal(80)], score=(1, 0)),
        fixture([], score=(0, 0)),
        fixture([goal(80)], score=(2, 0)),
    ]
    with conn.transaction():
        for i, item in enumerate(observations):
            save_timeline(conn, mid, 'api_football', '10', api_timeline(item),
                          NOW + timedelta(minutes=i), item)
        relate_goals(conn, rows(conn, 'SELECT * FROM live_signal_tracking')[0], 'api_football')
    assert conn.execute("SELECT count(*) FROM live_signal_goals_selected WHERE relation='after'").fetchone() == (0,)


def test_partial_own_goal_without_credited_side_cannot_duplicate_known_goal(conn):
    mid = signal(conn, 1)
    event = goal(80, side=2, detail='Own Goal')
    with conn.transaction():
        for i, score in enumerate([(1, 0), (2, 0)]):
            item = fixture([event], score=score)
            save_timeline(conn, mid, 'api_football', '10', api_timeline(item),
                          NOW + timedelta(minutes=i), item)
        relate_goals(conn, rows(conn, 'SELECT * FROM live_signal_tracking')[0], 'api_football')
    assert conn.execute("SELECT count(*) FROM live_signal_goals_selected WHERE relation='after'").fetchone() == (1,)


def test_completed_result_does_not_validate_different_score_from_live_widget():
    result_html = (FIXTURES / 'completed-result.html').read_text()
    live_html = (FIXTURES / 'timeline-seven-goals.html').read_text()
    # Mock an internally consistent stale live widget for the same event. The
    # completed result reports 0:1, the widget header and timeline report 7:0.
    data = {'completion_html': result_html, 'html': live_html,
            'clock_text': '83:41', 'period_text': '2 тайм', 'source_event_id': '69626793'}
    runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(data))
    timeline, _, _ = load_fonbet_timeline('https://fon.bet/live/football/11978/69626793', runner=runner)
    assert timeline.status == 'REG_END'
    assert not timeline.complete, f'stale live widget was accepted as complete, score={timeline.regulation_score}'


def test_missing_api_fixture_does_not_suppress_fonbet_fallback(conn, tmp_path):
    first_signal(conn)
    client = make_client(tmp_path, [{'response': []}])
    calls = []
    def load(url, **kwargs):
        calls.append(url)
        return Timeline(status='REG_END', reason='timeline_unavailable'), None, {}
    run_results(conn, client, fonbet_loader=load, now_fn=lambda: NOW + timedelta(minutes=45))
    assert len(calls) == 1 and conn.execute('SELECT status FROM live_signal_tracking').fetchone() == ('preliminary',)


def test_api_quota_does_not_block_fonbet_only_due_match(conn, tmp_path):
    configure(conn, tracking=True, reminders=False, now=NOW)
    mid = signal(conn, 1, review=True)
    add_fonbet(conn)
    conn.execute("INSERT INTO live_alert_aliases VALUES('fonbet','123',%s)", (mid,))
    client = make_client(tmp_path, [])
    client.quota_exhausted = True
    calls = []
    def load(url, **kwargs):
        calls.append(url)
        return Timeline(status='REG_END', reason='timeline_unavailable'), None, {}
    run_results(conn, client, fonbet_loader=load, now_fn=lambda: NOW + timedelta(days=1))
    assert len(calls) == 1 and conn.execute('SELECT review_day FROM live_signal_tracking').fetchone() == (1,)
