const {test} = require('node:test');
const assert = require('node:assert/strict');
const {candidate,count,normalize} = require('./core.cjs');
const event = {fonbet_event_id:'123',home_name:'Home',away_name:'Away',competition_name:'Лига. U19',clock_text:'70:12'};
const raw = {...event,period_text:'2 тайм',source_period_label:'2 тайм',source_metric_label:'Удары в створ',
  period_confirmed:true,home_value:'0',away_value:'4',requested_at:'2026-11-10T10:00:00Z',
  context_observed_at:'2026-11-10T10:00:01Z',observed_at:'2026-11-10T10:00:02Z'};
test('window includes 65 and 75, excludes 64,76, ET and esports',()=>{
  for (const [clock,expected] of [['64:59',false],['65:00',true],['75:59',true],['76:00',false],['45+2',false]])
    assert.equal(candidate({...event,clock_text:clock}),expected);
  assert.equal(candidate({...event,period_text:'Перерыв'}),false);
  assert.equal(candidate({...event,competition_name:'FC 26. ESportsBattle'}),false);
  assert.equal(candidate({...event,competition_name:'3x3. Микрофутзал'}),false);
});
test('zero, missing away and summed shots stay distinct',()=>{
  assert.equal(count('0'),0); assert.equal(count('—'),null);
  assert.equal(normalize(event,raw).shots_on_target_2h_total,4);
  const partial=normalize(event,{...raw,away_value:'—'});
  assert.equal(partial.shots_on_target_2h_total,null);assert.equal(partial.quality_status,'partial');
});
test('never substitute match or first-half or total shots',()=>{
  for(const source_period_label of ['Матч','1 тайм'])
    assert.throws(()=>normalize(event,{...raw,source_period_label}),/invalid_period/);
  assert.throws(()=>normalize(event,{...raw,source_metric_label:'Удары'}),/invalid_period/);
  assert.throws(()=>normalize(event,{...raw,period_confirmed:false}),/unconfirmed/);
});
test('reject swapped teams, stale context, empty and minute advanced outside window',()=>{
  assert.throws(()=>normalize(event,{...raw,home_name:'Away',away_name:'Home'}),/team_mismatch/);
  assert.throws(()=>normalize(event,{...raw,observed_at:'2026-11-10T10:00:32Z'}),/context_stale/);
  assert.throws(()=>normalize(event,{...raw,home_value:'—',away_value:''}),/empty/);
  assert.throws(()=>normalize(event,{...raw,clock_text:'76:00'}),/outside_window/);
  assert.throws(()=>normalize(event,{...raw,home_value:'1.5'}),/invalid_statistic/);
});

test('owner exclusions: FC prefix, futsal, microfutsal and 2X40 tags',()=>{
 for(const competition_name of ['FC. Турнир','FC PRO','fc27. Турнир','  Fc League','Микрофутзал. Лига','Футзал. Чемпионат','Юниоры. 2X40'])
  assert.equal(candidate({...event,competition_name}),false,competition_name);
 for(const tag of ['2X40','2x40','2Х40','2х40','2×40','2 X 40'])
  assert.equal(candidate({...event,format_tags:[tag]}),false,tag);
 assert.equal(candidate({...event,period_text:'2X40'}),false);
 assert.equal(candidate({...event,competition_name:'Лига FC. Чемпионат'}),true);
 assert.equal(candidate({...event,format_tags:['2X45']}),true);
 assert.equal(candidate({...event,format_tags:['12X40']}),true);
 assert.equal(candidate({...event,format_tags:['2X400']}),true);
});

test('half-score context is retained without incrementing raw shots',()=>{
 const value=normalize(event,{...raw,goals_home:3,goals_away:1,ht_home:1,ht_away:1});
 assert.equal(value.ht_home,1);assert.equal(value.shots_on_target_2h_away,4);
 for(const ht_home of [-1,4,true,'1'])
  assert.throws(()=>normalize(event,{...raw,goals_home:3,ht_home}),/invalid_half_score/);
});
