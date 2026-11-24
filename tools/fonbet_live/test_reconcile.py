import copy
import unittest
import io
import subprocess
from contextlib import redirect_stdout
from unittest.mock import patch

from reconcile import choose_match, decision_sql, name_key, main, database

EVENT=dict(fonbet_event_id='1',observation_id='00000000-0000-4000-8000-000000000001',
 home_name='Home U20',away_name='Away U20',competition_name='Youth League',elapsed=75,
 goals_home=1,goals_away=0,observed_at='2026-11-10T10:00:00Z',kickoff_at=None)
FIXTURE=dict(fixture_id=10,home_team_id=1,away_team_id=2,league_id=3,
 home_name='Home U20',away_name='Away U20',league_name='Youth League',kickoff_at='2026-11-10T08:30:00Z',
 snapshot=dict(elapsed=74,observed_at='2026-11-10T09:59:00Z',goals_home=1,goals_away=0))


class ReconcileTests(unittest.TestCase):
 def test_exact_and_reversed(self):
  self.assertEqual(choose_match(EVENT,[FIXTURE],{}, {})['fixture_id'],10)
  event=dict(EVENT,home_name=EVENT['away_name'],away_name=EVENT['home_name'],goals_home=0,goals_away=1)
  self.assertTrue(choose_match(event,[FIXTURE],{}, {})['home_away_swapped'])
 def test_missing_kickoff_requires_live_evidence(self):
  for change in (dict(snapshot=None),dict(snapshot=dict(FIXTURE['snapshot'],goals_home=2)),
                 dict(snapshot=dict(FIXTURE['snapshot'],observed_at='2026-11-10T09:55:00Z'))):
   self.assertEqual(choose_match(EVENT,[dict(FIXTURE,**change)],{}, {})['mapping_status'],'unmatched')
 def test_explicit_kickoff_and_ambiguity(self):
  event=dict(EVENT,kickoff_at=FIXTURE['kickoff_at'])
  self.assertEqual(choose_match(event,[dict(FIXTURE,snapshot=None)],{}, {})['fixture_id'],10)
  self.assertEqual(choose_match(event,[FIXTURE,dict(FIXTURE,fixture_id=11)],{}, {})['mapping_status'],'ambiguous')
  self.assertEqual(choose_match(dict(event,kickoff_at='2026-11-10T07:00:00Z'),[FIXTURE],{}, {})['mapping_status'],'unmatched')
 def test_aliases_and_category_protection(self):
  translated=dict(EVENT,home_name='Хозяева U20',away_name='Гости U20',competition_name='Молодёжь')
  self.assertEqual(choose_match(translated,[FIXTURE],{}, {})['mapping_status'],'unmatched')
  aliases={name_key('Хозяева U20'):1,name_key('Гости U20'):2}
  self.assertEqual(choose_match(translated,[FIXTURE],aliases,{name_key('Молодёжь'):3})['fixture_id'],10)
  for name in ('Home','Home Women','Home U21','Home Reserves'):
   self.assertEqual(choose_match(dict(EVENT,home_name=name),[FIXTURE],{}, {})['mapping_status'],'unmatched')
 def test_sql_checks_version_and_preserves_confirmation(self):
  sql=decision_sql(EVENT,choose_match(EVENT,[FIXTURE],{}, {}))
  self.assertIn("f.mapping_status <> 'confirmed'",sql)
  self.assertIn('f.observation_id=d.observation_id',sql)
 def test_cli_accepts_multiline_postgres_json(self):
  payload='"events":[], "fixtures":[], "team_aliases":{}, "league_aliases":{}'
  with patch("sys.argv", ["reconcile.py"]), patch("reconcile.database", return_value="{\n"+payload+"\n}\n"), redirect_stdout(io.StringIO()) as out:
   main()
  self.assertIn('"confirmed": 0', out.getvalue())
 def test_database_errors_do_not_expose_sql_or_parameters(self):
  for stderr,code in [('ERROR: canceling statement due to statement timeout','matching_query_timeout'),('secret SQL and private payload','matching_database_error')]:
   error=subprocess.CalledProcessError(3,['private command'],stderr=stderr)
   with patch('reconcile.subprocess.run',side_effect=error):
    with self.assertRaisesRegex(RuntimeError,'^'+code+'$'):database('private SQL')
 def test_punctuation_case_only(self):
  self.assertEqual(name_key(' Home, U20 '),name_key('HOME U20'))
  self.assertNotEqual(name_key('Home U20'),name_key('Home U21'))


if __name__=='__main__':unittest.main()
