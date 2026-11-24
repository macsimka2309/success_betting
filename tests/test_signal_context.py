import os
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace

import pytest
from tools.fonbet_live.match_clock import fonbet_clock
from tools.fonbet_live.store import build_context_sql
from src.jobs.live_context import record_api_context
from tests.test_collect import conn  # noqa: F401

NOW=datetime(2026,11,19,tzinfo=timezone.utc)
DB=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='disposable DB required')

@pytest.mark.parametrize('clock,label,expected',[
 ('75:00','2 тайм',('2H',75,None,'regulation_2h')),
 ('80:00','2 тайм',('2H',80,None,'regulation_2h')),
 ('90+12','2 тайм',('2H',90,12,'regulation_2h')),
 ('45+3','1 тайм',('1H',45,3,'regulation_1h')),
 ('105+1','доп. тайм',('ET',105,1,'extra_time')),
 ('90+12','Матч завершён',('FT',90,12,'finished')),
 ('92:00','2 тайм',('unknown',92,None,'unknown')),
 ('abc','',('unknown',None,None,'unknown')),
])
def test_clock(clock,label,expected):assert fonbet_clock(clock,label)==expected

@DB
def test_context_receipts_and_absence(conn):
    receipt=SimpleNamespace(requested_at=(NOW-timedelta(seconds=1)).timestamp(),observed_at=NOW.timestamp(),observation_id=123)
    record_api_context(conn,[{'fixture':{'id':1,'status':{'short':'2H','elapsed':90,'extra':12}}}],receipt)
    assert conn.execute('SELECT poll_id,period,added_minute,requested_at FROM live_alert_context').fetchone()==('123','regulation_2h',12,NOW-timedelta(seconds=1))
    record_api_context(conn,[],SimpleNamespace(observed_at=(NOW+timedelta(seconds=1)).timestamp()))
    assert conn.execute('SELECT status,present FROM live_alert_context').fetchone()==('absent',False)

@DB
def test_fonbet_export_context(conn):
    conn.execute(build_context_sql(dict(run_id='poll1',started_at=NOW.isoformat(),contexts=[dict(fonbet_event_id='123',clock_text='90+12',period_text='2 тайм',observed_at=(NOW+timedelta(seconds=1)).isoformat())])))
    assert conn.execute('SELECT poll_id,period,added_minute FROM live_alert_context').fetchone()==('poll1','regulation_2h',12)

@DB
def test_old_export_still_imports_after_additive_migration(conn):
    from psycopg.types.json import Jsonb
    old={'source':'fonbet','source_event_id':'legacy','observed_at':NOW.isoformat(),
         'status':'2H','elapsed':70,'present':True,'format_tags':[]}
    conn.execute('INSERT INTO live_alert_context SELECT * FROM jsonb_populate_recordset(NULL::live_alert_context,%s)',(Jsonb([old]),))
    assert conn.execute('SELECT period,poll_id FROM live_alert_context').fetchone()==(None,None)
