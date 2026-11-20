"""All DB tests use TEST_DATABASE_URL pointed at disposable PostgreSQL only."""
from datetime import datetime,timedelta,timezone
import json
import os
import subprocess
from types import SimpleNamespace

import psycopg
from psycopg.types.json import Jsonb
import pytest

from src.jobs import alert_dispatch as ad
from src.jobs.alert_rules import pressure,choose,usable
from src.jobs.live_context import UPSERT,record_api_context
from tests.test_collect import conn  # noqa: F401
from tests.test_live_source_matching import seed_api,add_api_statistics,add_fonbet

NOW=datetime(2026,11,10,10,0,5,tzinfo=timezone.utc)
pytestmark=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='disposable database required')

def context(conn,source,eid,minute=70,status='2H',when=NOW,tags=()):
    conn.execute(UPSERT,(source,str(eid),when,status,minute,Jsonb(list(tags))))


def api(conn,h=6,a=0,when=NOW):
    seed_api(conn);add_api_statistics(conn)
    stats={side:{'2h':{'shots_on_goal':v}} for side,v in [('home',h),('away',a)]}
    conn.execute('UPDATE live_match_statistics_75 SET statistics=%s,observed_at=%s WHERE fixture_id=10',(Jsonb(stats),when))
    context(conn,'api_football',10)


def fb(conn,h=6,a=0,eid='123',when=NOW):
    add_fonbet(conn,eid)
    conn.execute('UPDATE fonbet_live_match_statistics_75 SET shots_on_target_2h_home=%s,shots_on_target_2h_away=%s,quality_status=%s,observed_at=%s,requested_at=%s,context_observed_at=%s WHERE fonbet_event_id=%s',
      (h,a,'partial' if h is None or a is None else 'both_teams',when,when-timedelta(seconds=2),when-timedelta(seconds=1),eid))
    context(conn,'fonbet',eid)


def link(conn,eid='123',fid=10):
    conn.execute("UPDATE fonbet_live_match_statistics_75 SET fixture_id=%s,mapping_status='confirmed' WHERE fonbet_event_id=%s",(fid,eid))


def run(conn,sender=None,**kw):
    return ad.dispatch(conn,sender or (lambda text,markup:12345),now_fn=lambda:NOW,**kw)


@pytest.mark.parametrize('h,a,answer',[(4,4,True),(6,None,True),(None,6,True),(5,None,None),(5,0,False),(4,3,False),(4,None,None),(None,None,None),(0,0,False),(True,2,None)])
def test_three_valued_rule(h,a,answer):assert pressure(h,a) is answer


def test_fonbet_only_delivered_once_without_bet_buttons(conn):
    fb(conn)
    sent=[]
    assert run(conn,lambda t,m:sent.append((t,m)) or 17)['sent']==1
    assert sent[0][1] is None and 'Fonbet' in sent[0][0]
    assert conn.execute("SELECT state,telegram_message_id FROM live_alert_outbox").fetchone()==('sent',17)
    run(conn,lambda *a:pytest.fail('duplicate'))
    assert len(sent)==1


def test_late_mapping_does_not_replay_fonbet_as_api(conn):
    fb(conn);run(conn)
    api(conn);link(conn)
    run(conn,lambda *a:pytest.fail('late identity duplicate'))
    assert conn.execute('SELECT count(DISTINCT alert_match_id) FROM live_alert_aliases').fetchone()[0]==1


def test_api_negative_beats_positive_fonbet(conn):
    api(conn,4,3);fb(conn);link(conn)
    assert run(conn)['not_triggered']==1
    assert conn.execute('SELECT count(*) FROM live_alert_outbox').fetchone()[0]==0


@pytest.mark.parametrize('kind',['stale','unknown'])
def test_fonbet_fallback(conn,kind):
    api(conn,4,None,when=NOW-timedelta(minutes=3) if kind=='stale' else NOW);fb(conn);link(conn)
    captured=[];assert run(conn,lambda t,m:captured.append(t) or 9)['sent']==1
    assert 'Источник: Fonbet' in captured[0]
    # Known API-ID still creates registry-compatible buttons and a legacy alert row.
    assert conn.execute('SELECT fixture_id FROM live_alerts_sent').fetchone()==(10,)


def test_api_positive_uses_api_buttons_and_legacy_id(conn):
    api(conn);fb(conn);link(conn)
    sent=[];run(conn,lambda t,m:sent.append((t,m)) or 11)
    assert 'Источник: API-Football' in sent[0][0]
    legacy=conn.execute('SELECT alert_id,fixture_id FROM live_alerts_sent').fetchone()
    assert legacy[1]==10
    assert sent[0][1]['inline_keyboard'][0][0]['callback_data'].startswith(f'bet:{legacy[0]}:')


@pytest.mark.parametrize('minute,status',[(64,'2H'),(76,'2H'),(70,'FT'),(70,'HT'),(70,'ET')])
def test_current_context_stops_retained_snapshot(conn,minute,status):
    fb(conn);context(conn,'fonbet',123,minute,status)
    assert run(conn).get('sent',0)==0


def test_newer_api_context_76_blocks_old_api_and_fresh_fonbet(conn):
    api(conn,when=NOW-timedelta(minutes=5));fb(conn);link(conn);context(conn,'api_football',10,76)
    assert run(conn).get('sent',0)==0


@pytest.mark.parametrize('age',[-1,121])
def test_future_or_old_observation_not_sent(conn,age):
    fb(conn,when=NOW-timedelta(seconds=age))
    assert run(conn).get('sent',0)==0


def test_queue_retries_definite_failure_but_never_unknown_delivery(conn):
    fb(conn)
    def rejected(*a):raise ad.DeliveryFailure(definite=True)
    assert run(conn,rejected)['retry']==1
    conn.execute("UPDATE live_alert_outbox SET next_attempt_at='2000-01-01'")
    def unknown(*a):raise ad.DeliveryFailure()
    assert run(conn,unknown)['uncertain']==1
    run(conn,lambda *a:pytest.fail('uncertain replay'))
    assert conn.execute('SELECT attempts FROM live_alert_outbox').fetchone()[0]==2


def test_pending_expires_after_new_negative_source(conn):
    api(conn)
    run(conn,lambda *a:(_ for _ in ()).throw(ad.DeliveryFailure(True)))
    conn.execute("UPDATE live_match_statistics_75 SET statistics=%s",(Jsonb({'home':{'2h':{'shots_on_goal':0}},'away':{'2h':{'shots_on_goal':0}}}),))
    run(conn,lambda *a:pytest.fail('negative queued alert'))
    assert conn.execute('SELECT state FROM live_alert_outbox').fetchone()[0]=='expired'


def test_crashed_sending_recovers_as_uncertain(conn):
    fb(conn);run(conn,limit=0)
    conn.execute("UPDATE live_alert_outbox SET state='sending',lease_until='2000-01-01'")
    assert run(conn,lambda *a:pytest.fail('crashed sender replay')).get('sent',0)==0
    assert conn.execute('SELECT state FROM live_alert_outbox').fetchone()[0]=='uncertain'


def test_dry_run_does_not_leave_aliases_queue_or_ids(conn):
    fb(conn)
    # Fonbet snapshot insert creates no identity until a confirmed mapping or dispatcher.
    baseline=conn.execute('SELECT count(*) FROM live_alert_matches').fetchone()[0]
    assert run(conn,dry_run=True)['would_trigger']==1
    assert conn.execute('SELECT count(*) FROM live_alert_outbox').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM live_alert_matches').fetchone()[0]==baseline


def test_ambiguous_fonbet_candidate_waits(conn):
    fb(conn)
    conn.execute("UPDATE fonbet_live_match_statistics_75 SET mapping_status='ambiguous',mapping_candidates='[10]'")
    run(conn,lambda *a:pytest.fail('ambiguous send'))
    assert conn.execute('SELECT state FROM live_alert_outbox').fetchone()[0]=='identity_ambiguous'


def test_multiple_events_and_reversed_sides(conn):
    api(conn,4,3);fb(conn,eid='123');fb(conn,eid='124');link(conn,'123');link(conn,'124')
    conn.execute("UPDATE fonbet_live_match_statistics_75 SET home_away_swapped=true WHERE fonbet_event_id='124'")
    assert run(conn).get('sent',0)==0
    # If API becomes unusable, one Fonbet observation is picked; aligned shots preserved.
    conn.execute("UPDATE live_match_statistics_75 SET observed_at=%s",(NOW-timedelta(minutes=5),))
    sent=[];run(conn,lambda t,m:sent.append(t) or 99)
    assert len(sent)==1


def test_global_delivery_lock_excludes_other_dispatcher(conn):
    with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as other:
        other.execute('SELECT pg_advisory_lock(27001)')
        assert run(conn)=={'skipped_previous_running':1}
        other.execute('SELECT pg_advisory_unlock(27001)')


def test_live_context_records_departure_but_older_scan_does_not_regress(conn):
    item={'fixture':{'id':10,'status':{'short':'2H','elapsed':76}}}
    record_api_context(conn,[item],SimpleNamespace(observed_at=NOW.timestamp()))
    record_api_context(conn,[],SimpleNamespace(observed_at=(NOW-timedelta(seconds=1)).timestamp()))
    assert conn.execute("SELECT present,elapsed FROM live_alert_context WHERE source='api_football'").fetchone()==(True,76)
    record_api_context(conn,[],SimpleNamespace(observed_at=(NOW+timedelta(seconds=1)).timestamp()))
    assert conn.execute("SELECT present FROM live_alert_context WHERE source='api_football'").fetchone()[0] is False


@pytest.mark.parametrize('reply,code,definite',[(json.dumps({'ok':False,'description':'NO'}),0,True),('',6,True),('',28,False),('broken',0,False)])
def test_transport_classifies_without_secret_leaks(reply,code,definite):
    def runner(argv,**kw):
        assert all('SECRET' not in arg for arg in argv)
        return SimpleNamespace(returncode=code,stdout=reply)
    send=ad.telegram_sender('SECRET',1,runner=runner)
    with pytest.raises(ad.DeliveryFailure) as error:send('hello',None)
    assert error.value.definite is definite and 'SECRET' not in str(error.value)


def test_transport_saves_message_id():
    send=ad.telegram_sender('SECRET',1,runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout='{"ok":true,"result":{"message_id":77}}'))
    assert send('hello',None)==77


def test_old_legacy_delivery_blocks_both_sources(conn):
    api(conn)
    from src.jobs.live_alerts import Alert,_record_sent,_reserve_alert_id
    alert=Alert(fixture_id=10,league_id=3,league='Youth League',country='',home='Home U20',away='Away U20',minute=70,goals_home=1,goals_away=0,team_side='home',possession=None,shots=5,opponent_shots=0,category='half_pressure',alert_id=_reserve_alert_id(conn))
    _record_sent(conn,alert)
    fb(conn);link(conn)
    run(conn,lambda *a:pytest.fail('legacy replay'))
    assert conn.execute('SELECT count(*) FROM live_alert_outbox').fetchone()[0]==0


def test_db_failure_after_telegram_success_is_not_replayed(conn,monkeypatch):
    api(conn);sent=[]
    def broken(*a):raise RuntimeError('simulated DB persistence failure')
    monkeypatch.setattr(ad,'record_legacy',broken)
    with pytest.raises(RuntimeError):run(conn,lambda *a:sent.append(1) or 9)
    assert conn.execute('SELECT state FROM live_alert_outbox').fetchone()[0]=='sending'
    conn.execute("UPDATE live_alert_outbox SET lease_until='2000-01-01'")
    run(conn,lambda *a:pytest.fail('post-send DB failure replay'))
    assert len(sent)==1
    assert conn.execute('SELECT state FROM live_alert_outbox').fetchone()[0]=='uncertain'


def test_migration_roundtrip_preserves_legacy_deliveries(conn):
    from src.db import migrate
    api(conn);run(conn)
    # Roll back dependent additions first, as an operator must do in a fresh DB.
    migrate.revert(conn,next(m for m in migrate.discover() if m.version=='036'))
    migrate.revert(conn,next(m for m in migrate.discover() if m.version=='035'))
    migrate.revert(conn,next(m for m in migrate.discover() if m.version=='034'))
    migrate.revert(conn,next(m for m in migrate.discover() if m.version=='033'))
    migrate.revert(conn,next(m for m in migrate.discover() if m.version=='032'))
    migrate.revert(conn,next(m for m in migrate.discover() if m.version=='031'))
    dependent=next(m for m in migrate.discover() if m.version=='030')
    migrate.revert(conn,dependent)
    migration=next(m for m in migrate.discover() if m.version=='027')
    migrate.revert(conn,migration)
    assert conn.execute("SELECT to_regclass('live_alert_outbox')").fetchone()[0] is None
    assert conn.execute('SELECT count(*) FROM live_alerts_sent').fetchone()[0]==1
    migrate.apply(conn,migration)
    assert conn.execute('SELECT count(*) FROM live_alert_matches WHERE sent_at IS NOT NULL').fetchone()[0]==1


def test_enabled_dispatcher_keeps_api_collection_but_disables_old_send(conn,tmp_path):
    from tests.test_collect import make_client
    from tests.test_live_alerts import live_item
    from tests.test_live_statistics import body
    from src.jobs.live_alerts import run_live_alerts
    conn.execute('UPDATE live_alert_dispatch_config SET enabled=true')
    client=make_client(tmp_path,[{'response':[live_item(1,minute=70)]},body(5,0)])
    run_live_alerts(client,conn,lambda *a:pytest.fail('legacy sender active'))
    assert conn.execute('SELECT count(*) FROM live_match_statistics_75').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM live_alert_context').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM live_alerts_sent').fetchone()[0]==0


def test_config_rollback_still_respects_late_mapped_fonbet_sent(conn):
    from src.jobs.live_alerts import _already_sent
    fb(conn);run(conn);api(conn);link(conn)
    conn.execute('UPDATE live_alert_dispatch_config SET enabled=false')
    assert _already_sent(conn,[10],'half_pressure')=={10}


def test_fonbet_context_export_keeps_76_and_partial_scan_does_not_close_others(conn):
    from tools.fonbet_live.store import build_context_sql
    context(conn,'fonbet',999)
    data={'started_at':NOW.isoformat(),'context_scan_at':(NOW+timedelta(seconds=1)).isoformat(),
          'context_complete':False,'contexts':[{'fonbet_event_id':'123','observed_at':NOW.isoformat(),'clock_text':'76:01','period_text':''}]}
    conn.execute(build_context_sql(data))
    assert conn.execute("SELECT elapsed FROM live_alert_context WHERE source='fonbet' AND source_event_id='123'").fetchone()==(76,)
    assert conn.execute("SELECT present FROM live_alert_context WHERE source='fonbet' AND source_event_id='999'").fetchone()==(True,)
    data['context_complete']=True;data['started_at']=(NOW+timedelta(seconds=1)).isoformat()
    conn.execute(build_context_sql(data))
    assert conn.execute("SELECT present FROM live_alert_context WHERE source='fonbet' AND source_event_id='999'").fetchone()==(False,)


def test_provider_absence_does_not_hide_fresh_fonbet(conn):
    api(conn,when=NOW-timedelta(minutes=5));fb(conn);link(conn)
    conn.execute("UPDATE live_alert_context SET present=false,status='absent' WHERE source='api_football'")
    assert run(conn)['sent']==1
