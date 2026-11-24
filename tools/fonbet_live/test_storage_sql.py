"""Emit integration SQL for psql. Creates an isolated schema inside a rolled-back transaction."""
import copy
from pathlib import Path
from uuid import uuid4

from store import build_sql
from test_store import ROW


def script():
    schema='fonbet_live_test_'+uuid4().hex
    ddl=(Path(__file__).resolve().parents[2]/'migrations/025_fonbet_live_statistics.sql').read_text()
    parts=['BEGIN;',f'CREATE SCHEMA {schema};',f'SET LOCAL search_path TO {schema};',ddl]
    def assert_sql(condition, message):
        return f"DO $$ BEGIN IF NOT ({condition}) THEN RAISE EXCEPTION '{message}'; END IF; END $$;"
    for i,(minute,home,away) in enumerate([(70,0,4),(75,2,None),(72,9,9),(75,3,6),(75,1,2)],1):
        row=copy.deepcopy(ROW)
        row.update(elapsed=minute,clock_text=f'{minute}:00',shots_on_target_2h_home=home,
                   shots_on_target_2h_away=away,quality_status='partial' if away is None else 'both_teams',
                   run_id=f'00000000-0000-4000-8000-{i:012}',observation_id=f'10000000-0000-4000-8000-{i:012}',
                   observed_at=f'2026-11-10T10:00:0{i+1}Z')
        data=dict(run_id=row['run_id'],started_at=row['requested_at'],finished_at=row['observed_at'],
                  status='complete',summary={},observations=[row])
        sql=build_sql(data).removeprefix('BEGIN;').removesuffix('COMMIT;\n')
        parts.append(sql)
        if i==2:
            parts.append(assert_sql("EXISTS(SELECT 1 FROM fonbet_live_match_statistics_75 WHERE elapsed=75 AND shots_on_target_2h_home=2 AND shots_on_target_2h_away IS NULL AND shots_on_target_2h_total IS NULL)",'mixed_partial_snapshot'))
            parts.append(sql) # exact replay must not create a new observation or update snapshot
        if i==3:
            parts.append(assert_sql("EXISTS(SELECT 1 FROM fonbet_live_match_statistics_75 WHERE elapsed=75 AND shots_on_target_2h_home=2)",'older_overwrite'))
    parts.append(assert_sql("(SELECT count(*) FROM fonbet_live_observations)=5 AND EXISTS(SELECT 1 FROM fonbet_live_match_statistics_75 WHERE elapsed=75 AND shots_on_target_2h_total=3)",'identity_or_correction_failed'))
    parts.append('ROLLBACK;')
    return '\n'.join(parts)

if __name__=='__main__':print(script())
