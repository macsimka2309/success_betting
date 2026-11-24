const {test}=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {eventRows,statistic,selectedPeriod}=require('./dom.cjs');
// Representative sports-only markup from the inspected live UI; no account data.
const markup=`<div class="virtual-list--sample"><div style="top:30px"><div class="sport-competition--sample"><div class="table-component-text--sample">Лига U19</div></div></div>
<div style="top:62px"><div class="sport-base-event--sample"><a href="/live/football/9/123">Home — Away</a><span class="event-block-current-time__time--sample">70:12</span><span class="event-block-score--sample">0:0</span><span class="format-tag">2X40</span></div></div></div>
<div id="widget"><div class="rowTeam--sample"><div class="colName--sample">Home</div><div class="_large--sample">0</div></div><div class="rowTeam--sample"><div class="colName--sample">Away</div><div class="_large--sample">0</div></div>
<span class="scoreboard-timer__value--sample">70:14</span><span class="_selected--sample">2 тайм</span>
<div class="value-row--sample"><div class="_v1--sample">99</div><div class="caption--sample">Ударов всего</div><div class="_v2--sample">88</div></div>
<div class="value-row--sample"><div class="_v1--sample">0</div><div class="caption--sample">Ударов в створ</div><div class="_v2--sample">4</div></div></div>
<div class="value-row--other"><div class="_v1--other">77</div><div class="caption--other">Ударов в створ</div><div class="_v2--other">66</div></div>`;
test('sports DOM extraction stays scoped and distinguishes period and metric',async()=>{
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();
 try{
  await page.setContent(markup);
  const [event]=await page.locator('.virtual-list--sample').evaluate(eventRows);
  assert.equal(event.competition_name,'Лига U19');assert.equal(event.fonbet_event_id,'123');assert.equal(event.goals_home,0);assert.deepEqual(event.format_tags,['2X40']);
  const frame=page.locator('#widget');
  assert.equal(await frame.evaluate(selectedPeriod),'2 тайм');
  const stats=await frame.evaluate(statistic);assert.equal(stats.home_value,'0');assert.equal(stats.away_value,'4');
  await frame.locator('._selected--sample').evaluate(e=>e.textContent='Матч');
  assert.equal(await frame.evaluate(selectedPeriod),null);
  await frame.locator('._selected--sample').evaluate(e=>e.textContent='2 тайм');
  await frame.locator('div.caption--sample').last().evaluate(e=>e.textContent='Удары мимо');
  await assert.rejects(frame.evaluate(statistic),/metric_missing_or_ambiguous/);
 }finally{await browser.close();}
});

test('full browser path selects second half and rejects missing periods',async()=>{
 const {readEvent}=require('./collect.cjs');
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();
 const listMarkup=markup.slice(0,markup.indexOf('<div id="widget">')).replace('<span class="format-tag">2X40</span>','').replace('</a>',`</a><button resource-name="translation_mc_soccer" onclick="document.getElementById('frame').style.display='block'">Center</button>`);
 const fixture=`<meta charset="utf-8"><div class="sport-section-virtual-list--fixture">${listMarkup}</div>
 <div class="frame--fixture" style="display:none" id="frame">
 <div class="header--fixture"><span class="button--fixture" onclick="document.getElementById('stats-tab').style.display='block'">Expand</span></div>
 <div class="score-block--fixture"><div class="rowTeam--fixture"><div class="colName--fixture">Home</div></div><div class="rowTeam--fixture"><div class="colName--fixture">Away</div></div></div>
 <span id="stats-tab" style="display:none" onclick="document.getElementById('second').style.display='block'">Статистика</span>
 <span id="second" style="display:none" onclick="this.className='_selected--fixture'">2 тайм</span>
 <span class="scoreboard-timer__value--fixture">70:14</span>
 <div class="value-row--fixture"><div class="_v1--fixture">0</div><div class="caption--fixture">Ударов в створ</div><div class="_v2--fixture">4</div></div></div>`;
 try{
  await page.route('https://fon.bet/live/football',r=>r.fulfill({contentType:'text/html; charset=utf-8',body:fixture}));
  const event={fonbet_event_id:'123',event_url:'https://fon.bet/live/football/9/123',home_name:'Home',away_name:'Away',competition_name:'Лига U19',clock_text:'70:12'};
  const result=await readEvent(page,event);
  assert.equal(result.shots_on_target_2h_home,0);assert.equal(result.shots_on_target_2h_away,4);
  assert.equal(result.elapsed,70);assert.equal(result.source_period_label,'2 тайм');
  await page.unroute('https://fon.bet/live/football');
  await page.route('https://fon.bet/live/football',r=>r.fulfill({contentType:'text/html; charset=utf-8',body:fixture.replace('>2 тайм<','>Матч<')}));
  await assert.rejects(readEvent(page,event),/second_half_unavailable/);
 }finally{await browser.close();}
});

test('real Fonbet selected statistics tab uses tabs-item__select marker',async()=>{
  const browser=await chromium.launch({headless:true});
  const page=await browser.newPage();
  try{
    await page.setContent('<div id="root"><div class="tabs-item--x">Матч</div><div class="tabs-item--x">1-й тайм</div><div class="tabs-item--x tabs-item__select--x">2-й тайм</div></div>');
    assert.equal(await page.locator('#root').evaluate(selectedPeriod),'2-й тайм');
  }finally{await browser.close();}
});

test('missing target never returns a different live event and reused list avoids reload',async()=>{
 const {discover,readEvent}=require('./collect.cjs');
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();let navigations=0;
 try{
 await page.route('https://fon.bet/live/football',r=>{navigations++;return r.fulfill({contentType:'text/html; charset=utf-8',body:'<div class="sport-section-virtual-list--fixture">'+markup.slice(0,markup.indexOf('<div id="widget">'))+'</div>'});});
 const result=await discover(page,'999');assert.deepEqual(result.events,[]);assert.equal(result.complete,true);
 await discover(page,'123',{reuse:true});assert.equal(navigations,1);
 await assert.rejects(readEvent(page,{fonbet_event_id:'999'},{},{reuse:true}),/event_disappeared/);
 assert.equal(navigations,1);
 }finally{await browser.close();}
});

test('halftime score comes only from verified period headers, not cards or corners',async()=>{
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();
 const block=(v,klass='cell')=>`<div class="colBlock--s">${v.map(x=>`<div class="${klass}--s">${x}</div>`).join('')}</div>`;
 const team=(name,score,periods)=>`<div class="rowTeam--s"><div class="colName--s">${name}</div><div class="colBlock--s"><div class="cell--s _large--s">${score}</div></div>${block([9,8])}${block(periods)}</div>`;
 const score=`<div class="score-block--s"><div class="rowHeader--s">${block([''],'head')}${block(['cards','corners'],'head')}${block(['1','2'],'head')}</div>${team('Home',3,[1,2])}${team('Away',1,[1,0])}</div>`;
 try{
 await page.setContent('<div id="frame">'+score+'<div class="value-row--s"><span class="_v1--s">4</span><span class="caption--s">Ударов в створ</span><span class="_v2--s">2</span></div></div>');
 const frame=page.locator('#frame');let data=await frame.evaluate(statistic);
 assert.equal(data.ht_home,1);assert.equal(data.ht_away,1);assert.equal(data.goals_home,3);assert.equal(data.home_value,'4');
 await frame.locator('[class*="rowHeader--"] [class*="head--"]').last().evaluate(e=>e.textContent='3');
 data=await frame.evaluate(statistic);assert.equal(data.ht_home,null);assert.equal(data.ht_away,null);
 await frame.locator('[class*="rowHeader--"] [class*="head--"]').last().evaluate(e=>e.textContent='2');
 await frame.locator('[class*="rowTeam--"]').first().locator('[class*="_large--"]').evaluate(e=>e.textContent='0');
 data=await frame.evaluate(statistic);assert.equal(data.ht_home,null);assert.equal(data.ht_away,1);
 }finally{await browser.close();}
});

test('nested wrapper is excluded but two actual widgets remain ambiguous',async()=>{
 const b=await chromium.launch({headless:true});const p=await b.newPage();
 try{
 await p.setContent('<div class="outer-frame--a"><div class="frame--b"><div class="score-block--s"></div></div></div>');
 const frames=p.locator('[class*="frame--"]').filter({has:p.locator('[class*="score-block--"]')});const leaf=frames.filter({hasNot:frames});
 assert.equal(await frames.count(),2);assert.equal(await leaf.count(),1);
 await p.locator('body').evaluate(e=>e.insertAdjacentHTML('beforeend','<div class="frame--c"><div class="score-block--s"></div></div>'));
 assert.equal(await leaf.count(),2);await assert.rejects(leaf.waitFor({timeout:500}),/strict mode/);
 }finally{await b.close();}
});

test('verified close control removes old widget and unknown layouts are left untouched',async()=>{
 const {closeCenter}=require('./collect.cjs');const b=await chromium.launch({headless:true});const p=await b.newPage();
 try{
 const widget=n=>'<div class="frame--s"><div class="score-block--s"></div><div class="header--s">'+Array.from({length:n},(_,i)=>'<span class="button--s" '+(i===2?'onclick="this.closest(\'.frame--s\').remove()"':'')+'>x</span>').join('')+'</div></div>';
 await p.setContent(widget(3));await closeCenter(p);assert.equal(await p.locator('.frame--s').count(),0);
 await p.setContent(widget(2));await assert.rejects(closeCenter(p),/cleanup_unavailable/);assert.equal(await p.locator('.frame--s').count(),1);
 await p.setContent(widget(3)+widget(3));await assert.rejects(closeCenter(p),/ambiguous/);assert.equal(await p.locator('.frame--s').count(),2);
 }finally{await b.close();}
});

test('country-prefixed football routes preserve original URL and source event ID',async()=>{
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();
 try{
  const path='/live/football/country/czechrepublic/20285/69891885';
  await page.setContent(markup.replace('/live/football/9/123',path));
  const [event]=await page.locator('.virtual-list--sample').evaluate(eventRows);
  assert.equal(event.fonbet_event_id,'69891885');assert.equal(event.event_url,'https://fon.bet'+path);
  await page.locator('.virtual-list--sample a').evaluate(e=>e.setAttribute('href','/live/hockey/country/czechrepublic/20285/69891885'));
  assert.deepEqual(await page.locator('.virtual-list--sample').evaluate(eventRows),[]);
 }finally{await browser.close();}
});

test('link audit includes unsupported, malformed and missing links without query secrets',async()=>{
 const b=await chromium.launch({headless:true});const p=await b.newPage();
 const row=(href,label)=>`<div class="sport-base-event--s">${href===null?label:`<a href="${href}">${label}</a>`}</div>`;
 try{
 await p.setContent('<div class="virtual-list--s">'+
  row('/live/football/1/10','Home — Away')+
  row('/live/football/country/czechrepublic/1/11','Home — Away')+
  row('/live/football/new-route/1/12?token=SECRET#secret','Home — Away')+
  row('/live/football/1/13','Unrecognizable label')+row(null,'Missing link')+'</div>');
 const data=await p.locator('.virtual-list--s').evaluate(eventRows,true);
 assert.equal(data.events.length,2);assert.equal(data.rows.length,5);
 assert.deepEqual(data.rows.map(r=>r.issue),[null,null,'unsupported_event_url','invalid_team_names','missing_link']);
 assert.equal(data.rows[2].path,'/live/football/new-route/1/12');
 assert(!JSON.stringify(data).includes('SECRET'));
 }finally{await b.close();}
});

test('full traversal with an unknown route is incomplete and preserves known matches',async()=>{
 const {discover}=require('./collect.cjs');const b=await chromium.launch({headless:true});const p=await b.newPage();
 try{
 const fixture='<div class="sport-section-virtual-list--s"><div class="virtual-list--s">'+
  '<div class="sport-base-event--s"><a href="/live/football/1/10">Home — Away</a></div>'+
  '<div class="sport-base-event--s"><a href="/live/football/future-route/1/11">Other — Away</a></div></div></div>';
 await p.route('https://fon.bet/live/football',r=>r.fulfill({contentType:'text/html; charset=utf-8',body:fixture}));
 const result=await discover(p,null,{auditAll:true});
 assert.equal(result.traversal_complete,true);assert.equal(result.complete,false);
 assert.equal(result.link_audit.rows_seen,2);assert.equal(result.link_audit.unrecognized_rows,1);
 assert.equal(result.events[0].fonbet_event_id,'10');assert.equal(result.link_rows.length,2);
 }finally{await b.close();}
});

test('known cyberfootball category is audited as excluded and never collected',async()=>{
 const b=await chromium.launch({headless:true});const p=await b.newPage();
 try{
 await p.setContent('<div class="virtual-list--s"><div class="sport-base-event--s"><a href="/live/football/category/cyberfootball/148027/69879084">Home — Away</a></div></div>');
 const data=await p.locator('.virtual-list--s').evaluate(eventRows,true);
 assert.deepEqual(data.events,[]);assert.equal(data.rows[0].issue,null);
 assert.equal(data.rows[0].excluded_route,true);assert.equal(data.rows[0].format,'category_cyberfootball');
 await p.locator('a').evaluate(e=>e.setAttribute('href','/live/football/category/unknown/1/2'));
 assert.equal((await p.locator('.virtual-list--s').evaluate(eventRows,true)).rows[0].issue,'unsupported_event_url');
 }finally{await b.close();}
});


test('compact selections are excluded but match-like missing links remain errors',async()=>{
 const {summarizeLinks}=require('./collect.cjs');
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();
 const row=(name,extra='',classes='_compact--x _without-other-factors-count--x')=>`<div class="sport-base-event--x ${classes}"><div class="sport-event__name--x">${name}</div>${extra}</div>`;
 try{
  const selections=['Франция','Италия','Бельгия','Турция','Испания','Англия','Хорватия','Чехия'];
  await page.setContent('<div class="virtual-list--x">'+selections.map(n=>row(n,'<span>1.55</span><span>—</span>')).join('')+'</div>');
  let data=await page.locator('.virtual-list--x').evaluate(eventRows,true);
  assert.equal(data.events.length,0);assert.equal(data.rows.length,8);
  const audit=summarizeLinks(data.rows);
  assert.equal(audit.excluded_non_match_rows,8);assert.equal(audit.unrecognized_rows,0);
  assert.deepEqual(audit.formats,{compact_single_selection:8});
  const suspicious=[row('Home — Away'),row('Home - Away'),row('Home – Away'),
   row('Франция','<span class="event-block-score--x">0:0</span>'),
   row('Франция','<span class="event-block-current-time__time--x"></span>'),
   row('Франция','<span class="period--x">2 тайм</span>'),
   row('Франция','<button resource-name="translation_mc_soccer"></button>'),
   row('Франция','','_compact--x'),row('Франция','','_without-other-factors-count--x'),
   row(''),row('Франция','<span class="sport-event__name--x">Италия</span>'),
   row('Франция','<a href="/live/football/unknown/1/2">Home — Away</a>')];
  await page.setContent('<div class="virtual-list--x">'+suspicious.join('')+'</div>');
  data=await page.locator('.virtual-list--x').evaluate(eventRows,true);
  assert(data.rows.every(r=>!r.excluded_non_match));
  assert.deepEqual(data.rows.map(r=>r.issue),[...Array(11).fill('missing_link'),'unsupported_event_url']);
 }finally{await browser.close();}
});

test('internationalteams routes retain original IDs and participate in complete discovery',async()=>{
 const {discover,summarizeLinks}=require('./collect.cjs');
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();
 const path='/live/football/category/internationalteams/119266/69943522';
 try{
  const html=markup.slice(0,markup.indexOf('<div id="widget">')).replace('/live/football/9/123',path);
  await page.setContent(html);
  const data=await page.locator('.virtual-list--sample').evaluate(eventRows,true);
  assert.equal(data.events.length,1);assert.equal(data.events[0].fonbet_event_id,'69943522');
  assert.equal(data.events[0].event_url,'https://fon.bet'+path);
  assert.equal(data.rows[0].format,'category_internationalteams');
  assert.equal(summarizeLinks(data.rows).unrecognized_rows,0);
  for(const bad of [path.replace('internationalteams','unknown'),path.replace('internationalteams','internationalfutsal'),path+'/extra',path+'?x=1',path.replace('/football/','/hockey/')]){
   await page.locator('a').evaluate((e,href)=>e.setAttribute('href',href),bad);
   const result=await page.locator('.virtual-list--sample').evaluate(eventRows,true);
   assert.equal(result.events.length,0);assert.equal(result.rows[0].issue,'unsupported_event_url');
  }
  await page.route('https://fon.bet/live/football',r=>r.fulfill({contentType:'text/html; charset=utf-8',body:'<div class="sport-section-virtual-list--s">'+html+'</div>'}));
  const result=await discover(page,null,{auditAll:true});
  assert.equal(result.complete,true);assert.equal(result.events[0].fonbet_event_id,'69943522');
 }finally{await browser.close();}
});

test('internationalclubs routes are discovered without disabling unknown-route checks',async()=>{
 const {discover,summarizeLinks}=require('./collect.cjs');
 const b=await chromium.launch({headless:true});const p=await b.newPage();
 try{
 for(const id of ['69951675','69951677']){
  const path='/live/football/category/internationalclubs/133061/'+id;
  const html=markup.slice(0,markup.indexOf('<div id="widget">')).replace('/live/football/9/123',path);
  await p.setContent(html);const data=await p.locator('.virtual-list--sample').evaluate(eventRows,true);
  assert.equal(data.events[0].fonbet_event_id,id);assert.equal(data.events[0].event_url,'https://fon.bet'+path);
  assert.equal(data.rows[0].format,'category_internationalclubs');assert.equal(summarizeLinks(data.rows).unrecognized_rows,0);
  await p.route('https://fon.bet/live/football',r=>r.fulfill({contentType:'text/html; charset=utf-8',body:'<div class="sport-section-virtual-list--s">'+html+'</div>'}));
  const result=await discover(p,null,{auditAll:true});assert.equal(result.complete,true);assert.equal(result.events[0].fonbet_event_id,id);
  await p.unroute('https://fon.bet/live/football');
 }
 }finally{await b.close();}
});
