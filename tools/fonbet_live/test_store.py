import copy
import unittest
from store import build_sql, validate

RUN='00000000-0000-4000-8000-000000000001'
ROW=dict(fonbet_event_id='123',event_url='https://fon.bet/live/football/1/123',home_name="O'Home",away_name='Away',
 competition_name='Лига',period='2H',elapsed=75,clock_text='75:00',source_period_label='2 тайм',
 source_metric_label='Удары в створ',discovered_at='2026-11-10T10:00:00Z',requested_at='2026-11-10T10:00:00Z',
 context_observed_at='2026-11-10T10:00:01Z',observed_at='2026-11-10T10:00:02Z',minute_basis='match_center',
 shots_on_target_2h_home=0,shots_on_target_2h_away=4,quality_status='both_teams',
 observation_id='00000000-0000-4000-8000-000000000002',run_id=RUN,status='ok')
class StoreTests(unittest.TestCase):
 def test_internationalteams_url_preserves_identity_and_rejects_other_routes(self):
  url='https://fon.bet/live/football/category/internationalteams/119266/69943522'
  row=dict(ROW,event_url=url,fonbet_event_id='69943522')
  self.assertEqual(validate(row)['event_url'],url)
  self.assertEqual(validate(row)['fonbet_event_id'],'69943522')
  for invalid in (url.replace('internationalteams','cyberfootball'),url.replace('internationalteams','unknown'),
                  url.replace('internationalteams','internationalfutsal'),url.replace('fon.bet','evil.example'),
                  url.replace('/football/','/hockey/'),url.replace('https:','http:'),
                  url.replace('69943522','69943526'),url+'/extra',url+'?x=1',url+'#fragment'):
   with self.subTest(url=invalid), self.assertRaisesRegex(ValueError,'invalid_event_url'):
    validate(dict(row,event_url=invalid))
  data=dict(run_id=RUN,started_at=ROW['requested_at'],finished_at=ROW['observed_at'],status='complete',summary={},observations=[row])
  self.assertIn(url,build_sql(data))
 def test_internationalclubs_routes_preserve_source_ids(self):
  for event_id in ('69951675','69951677'):
   url='https://fon.bet/live/football/category/internationalclubs/133061/'+event_id
   row=dict(ROW,event_url=url,fonbet_event_id=event_id)
   self.assertEqual(validate(row)['event_url'],url)
   self.assertEqual(validate(row)['fonbet_event_id'],event_id)
   data=dict(run_id=RUN,started_at=ROW['requested_at'],finished_at=ROW['observed_at'],status='complete',summary={},observations=[row])
   self.assertIn(url,build_sql(data))
   for bad in (url.replace('internationalclubs','cyberfootball'),url.replace('internationalclubs','unknown'),url+'/extra',url+'?x=1',url.replace('fon.bet','evil.example')):
    with self.assertRaisesRegex(ValueError,'invalid_event_url'):validate(dict(row,event_url=bad))
 def test_validation_ignores_unreviewed_mapping(self):
  result=validate(dict(ROW,fixture_id=999,mapping_status='confirmed'))
  self.assertNotIn('fixture_id',result)
  self.assertEqual(result['shots_on_target_2h_home'],0)
 def test_stale_or_outside_window_rejected(self):
  for change in [dict(elapsed=76),dict(observed_at='2026-11-10T10:00:32Z'),dict(quality_status='partial'),dict(shots_on_target_2h_home=True),dict(source_period_label='Матч')]:
   with self.assertRaises(ValueError):validate(dict(ROW,**change))
 def test_sql_escapes_and_does_not_archive_unrelated_fields(self):
  data=dict(run_id=RUN,started_at=ROW['requested_at'],finished_at=ROW['observed_at'],status='complete',summary={},observations=[dict(ROW,account_text='PRIVATE_SENTINEL')])
  sql=build_sql(data)
  self.assertIn("O''Home",sql);self.assertNotIn('PRIVATE_SENTINEL',sql)
  self.assertIn('EXCLUDED.elapsed,EXCLUDED.observed_at,EXCLUDED.observation_id',sql)
 def test_partial_and_empty(self):
  self.assertIsNone(validate(dict(ROW,shots_on_target_2h_away=None,quality_status='partial'))['shots_on_target_2h_away'])
  with self.assertRaises(ValueError):validate(dict(ROW,shots_on_target_2h_home=None,shots_on_target_2h_away=None))
 def test_half_score_is_preserved_and_validated(self):
  result=validate(dict(ROW,goals_home=3,goals_away=1,ht_home=1,ht_away=1))
  self.assertEqual(result['ht_home'],1)
  self.assertEqual(result['shots_on_target_2h_home'],0)
  for ht in (-1,4,True,'1'):
   with self.assertRaises(ValueError):validate(dict(ROW,goals_home=3,ht_home=ht))
  with self.assertRaises(ValueError):validate(dict(ROW,ht_home=1))
 def test_country_event_route_and_exact_host_and_event_id(self):
  url='https://fon.bet/live/football/country/czechrepublic/20285/123'
  self.assertEqual(validate(dict(ROW,event_url=url))['event_url'],url)
  for invalid in (url.replace('fon.bet','evil.example'),url.replace('/football/','/hockey/'),url.replace('/123','/456'),url+'/extra',url.replace('https://','http://'),url.replace('/country/czechrepublic/','/category/cyberfootball/')):
   with self.assertRaises(ValueError):validate(dict(ROW,event_url=invalid))
 def test_run_identity(self):
  data=dict(run_id=RUN,started_at=ROW['requested_at'],finished_at=ROW['observed_at'],status='complete',summary={},observations=[dict(ROW,run_id='00000000-0000-4000-8000-000000000099')])
  with self.assertRaises(ValueError):build_sql(data)
if __name__=='__main__':unittest.main()
