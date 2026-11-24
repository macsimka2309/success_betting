"""Real SQL on disposable database: single freebets, matching and unit results."""
import importlib.util
import json
import os
import sys
from pathlib import Path
import pytest
from tests.test_collect import conn  # noqa: F401

spec=importlib.util.spec_from_file_location('freebet_import',Path(__file__).resolve().parents[1]/'tools/fonbet_sync/import_history.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
pytestmark=pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='disposable database required')


def coupon(label='Выигрыш',freebet=True):
    return dict(id='12345678901',type='Одинар\nФрибет' if freebet else 'Одинар',legs=[['13.11.2026 21:00','LIVE 1:3 Home – Away','> 4.5','1.65','2:3',label]],bar=['Одинар','500\n325','1.65','13.11.2026 22:31:30','Рассчитана 13.11.2026 22:36:47'])


def apply(conn,raw):
    row=mod.parse_coupon(raw)
    payload=json.dumps([row],ensure_ascii=False).replace("'","''")
    conn.execute(mod.SQL.replace('__DATA__',"'"+payload+"'").replace('__DAY__',"'2026-11-13'"))


@pytest.mark.parametrize('label,status,profit',[('Выигрыш','won','0.650'),('Проигрыш','lost','-1.000'),('Возврат','push','0.000')])
def test_freebet_saved_once_with_normalized_stake(conn,label,status,profit):
    for _ in range(2):apply(conn,coupon(label))
    rows=conn.execute('SELECT fonbet_coupon_id,is_freebet,status,stake,profit,manual_home_name FROM bets').fetchall()
    assert len(rows)==1
    assert rows[0][:3]==('12345678901',True,status)
    assert rows[0][3]==1 and str(rows[0][4])==profit and rows[0][5]=='Home'


def test_existing_settled_result_preserved_when_freebet_metadata_added(conn):
    apply(conn,coupon(freebet=False))
    apply(conn,coupon())
    row=conn.execute('SELECT is_freebet,status,profit FROM bets').fetchone()
    assert row[0] is True and row[1]=='won' and str(row[2])=='0.650'
    apply(conn,coupon('Проигрыш'))
    assert conn.execute('SELECT is_freebet,status,profit FROM bets').fetchone()==row
    # A subsequent export claiming ordinary funds cannot silently erase a confirmed flag.
    apply(conn,coupon(freebet=False))
    assert conn.execute('SELECT is_freebet,status,profit FROM bets').fetchone()==row


def test_manual_open_bet_linked_to_freebet_and_settled_without_duplicate(conn):
    conn.execute("INSERT INTO leagues(league_id,name) VALUES(1,'League')")
    conn.execute("INSERT INTO teams(team_id,name) VALUES(1,'Home'),(2,'Away')")
    conn.execute("INSERT INTO fixtures(fixture_id,league_id,season,kickoff_at,match_date,home_team_id,away_team_id) VALUES(1,1,2026,'2026-11-13 21:00+03','2026-11-13',1,2)")
    conn.execute("INSERT INTO bets(fixture_id,market,line,odds,stake,status,created_at,manual_home_name,manual_away_name) VALUES(1,'TB',4.5,1.65,1,'open','2026-11-13 22:31:30+03','Home','Away')")
    apply(conn,coupon())
    assert conn.execute('SELECT count(*),bool_and(is_freebet),min(status),min(fonbet_coupon_id) FROM bets').fetchone()==(1,True,'won','12345678901')


def test_nightly_import_keeps_only_match_overs(conn, tmp_path, monkeypatch, capsys):
    def entry(coupon_id, prediction, *, freebet=False, label='Выигрыш'):
        raw = coupon(label, freebet)
        raw.update(id=coupon_id, date='13.11')
        raw['legs'][0][2] = prediction
        return raw

    conn.execute('''
        INSERT INTO bets(fixture_id, market, line, odds, stake, status, created_at,
                         manual_home_name, manual_away_name, fonbet_coupon_id)
        VALUES (1, 'TB', 4.5, 1.65, 1, 'open', '2026-11-13 22:31:30+03', 'Home', 'Away', NULL),
               (1, 'TM', 4.5, 1.65, 1, 'open', '2026-11-12 22:31:30+03', 'Home', 'Away', '12345678903')
    ''')
    monkeypatch.setattr(mod, 'psql', lambda sql: '\n'.join(row[0] for row in conn.execute(sql)))
    monkeypatch.setattr(sys, 'argv', ['import_history.py', '--day', '2026-11-13', '--dates'])
    assert mod.main() is None
    assert json.loads(capsys.readouterr().out) == ['13.11']

    old_under = entry('12345678903', '< 4.5', label='Проигрыш')
    old_under['date'] = '12.11'
    old_under['bar'][3] = '12.11.2026 22:31:30'
    path = tmp_path / 'history.json'
    path.write_text(json.dumps({'complete': True, 'dates': ['12.11', '13.11'], 'rows': [
        entry('12345678901', '> 4.5'),
        entry('12345678902', '> 3.5', freebet=True),
        old_under,
        entry('12345678904', 'Home > 1.5', freebet=True),
    ]}))

    def execute(sql):
        with conn.cursor() as cur:
            cur.execute(sql)
            while not (cur.description and cur.description[0].name == 'row_to_json'):
                assert cur.nextset()
            return '\n'.join(json.dumps(row[0]) for row in cur.fetchall())

    monkeypatch.setattr(mod, 'psql', execute)
    monkeypatch.setattr(sys, 'argv', ['import_history.py', '--day', '2026-11-13', '--input', str(path), '--apply'])
    for ordinary_action, freebet_action in (('updated', 'added'), ('existing', 'existing')):
        assert mod.main() == 0
        report = json.loads(capsys.readouterr().out)
        assert (report['status'], report['found'], report['freebet_count'], report['skipped_count']) == ('complete', 2, 1, 0)
        assert {(row['coupon'], row['action']) for row in report['results']} == {
            ('12345678901', ordinary_action), ('12345678902', freebet_action),
        }
    assert conn.execute('''
        SELECT fonbet_coupon_id, market, is_freebet, status FROM bets ORDER BY fonbet_coupon_id
    ''').fetchall() == [
        ('12345678901', 'TB', False, 'won'),
        ('12345678902', 'TB', True, 'won'),
        ('12345678903', 'TM', False, 'open'),
    ]
