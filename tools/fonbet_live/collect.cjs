'use strict';
const {chromium}=require('playwright');
const fs=require('node:fs');
const {randomUUID}=require('node:crypto');
const {monitorMemory}=require('./memory.cjs');
const {candidate,normalize,standardFootball,clockMinute}=require('./core.cjs');
const {eventRows,scrollList,selectedPeriod,statistic}=require('./dom.cjs');
const now=()=>new Date().toISOString();
const LIST='https://fon.bet/live/football';
const FRAME='[class*="frame--"]';
const semanticCodes=new Set(['context_stale', 'discovery_incomplete', 'empty', 'event_disappeared', 'event_identity_mismatch', 'invalid_context', 'invalid_event', 'invalid_half_score', 'invalid_period_or_metric', 'invalid_statistic', 'invalid_teams', 'live_list_unavailable', 'match_center_ambiguous', 'match_center_cleanup_unavailable', 'match_center_unavailable', 'metric_missing_or_ambiguous', 'outside_window', 'second_half_unavailable', 'statistics_unavailable', 'team_mismatch', 'time_budget', 'unconfirmed_period']);
function reason(error){
  if(/strict mode violation/i.test(error.message))return 'selector_ambiguous';
  if(/not attached|detached/i.test(error.message))return 'element_detached';
  if(/Execution context was destroyed|navigation.*interrupted|net::ERR_ABORTED/i.test(error.message))return 'navigation_interrupted';
  if(/crash/i.test(error.message))return 'page_crashed';
  if(/Target.*closed|browser.*closed|Connection closed/i.test(error.message))return 'browser_closed';
  return semanticCodes.has(error.message)?error.message:error.name==='TimeoutError'?'timeout':'browser_error';
}
async function safeClose(object){
  if(!object)return;
  let timer;
  try{await Promise.race([object.close().catch(()=>{}),new Promise(resolve=>{timer=setTimeout(resolve,3000);})]);}
  catch{}finally{clearTimeout(timer);}
}
const retryable=code=>['page_crashed','browser_closed','browser_error','timeout','selector_ambiguous','element_detached','navigation_interrupted','invalid_context','match_center_ambiguous','match_center_cleanup_unavailable'].includes(code);
async function step(stage,work){try{return await work();}catch(error){error.collectorStage=stage;throw error;}}
function diagnostic(error,stage,eventId=null){
  // Raw Playwright messages/stacks can contain URLs or page contents: whitelist only.
  return {stage:['close_match_center','refresh_context','open_match_center','wait_match_center','select_statistics','select_second_half','read_statistics'].includes(error.collectorStage)?error.collectorStage:stage,event_id:eventId,code:reason(error),error_type:['Error','TimeoutError','TypeError','RangeError','TargetClosedError'].includes(error.name)?error.name:'Error',match_center_count:Number.isInteger(error.matchCenterCount)?error.matchCenterCount:null,changed_fields:Array.isArray(error.changedFields)?error.changedFields.filter(k=>['period','home_name','away_name','goals_home','goals_away','ht_home','ht_away','home_value','away_value'].includes(k)):[]};
}
async function openList(page,reuse=false){
  if(!reuse || page.url()!==LIST)await page.goto(LIST,{waitUntil:'domcontentloaded',timeout:45000});
  await page.locator('[class*="sport-section-virtual-list--"]').waitFor({timeout:20000});
  const list=page.locator('[class*="sport-section-virtual-list--"] [class*="virtual-list--"]').first();
  await list.waitFor();
  await Promise.race([
    list.locator('[class*="sport-base-event--"]').first().waitFor({timeout:30000}),
    page.getByText(/^(Нет событий|Событий нет|Сейчас нет событий)$/).first().waitFor({timeout:30000})
  ]);
  return list;
}
function summarizeLinks(rows){
  const unique=[...new Map(rows.map(row=>[row.key,row])).values()];
  const formats={},issues={};
  for(const row of unique){formats[row.format]=(formats[row.format]||0)+1;if(row.issue)issues[row.issue]=(issues[row.issue]||0)+1;}
  const unknown=unique.filter(row=>row.issue);
  return {rows_seen:unique.length,recognized_rows:unique.length-unknown.length,unrecognized_rows:unknown.length,
    excluded_route_rows:unique.filter(row=>row.excluded_route).length,
    excluded_non_match_rows:unique.filter(row=>row.excluded_non_match).length,
    formats,issues,samples:unknown.slice(0,10).map(({path,issue})=>({path,issue}))};
}
async function discover(page,targetId=null,{reuse=false,auditAll=false}={}){
  const list=await openList(page,reuse),events=new Map(),rows=new Map();
  const finish=traversalComplete=>{
    const link_audit=summarizeLinks([...rows.values()]);
    return {events:targetId?(events.has(targetId)?[events.get(targetId)]:[]):[...events.values()],
      complete:traversalComplete && link_audit.unrecognized_rows===0,traversal_complete:traversalComplete,link_audit,
      ...(auditAll?{link_rows:[...rows.values()]}:{})};
  };
  await list.evaluate(scrollList,true);
  let stable=0;
  for(let step=0;step<300;step++){
    // Only tournament expanders are clicked. Event/market expanders are never clicked.
    const collapsed=list.locator('[class*="sport-competition__expander--"][class*="_collapsed--"]');
    for(const expander of await collapsed.all()) {
      const header=expander.locator('..');
      const competition=await header.locator('[class*="table-component-text--"]').first().innerText();
      if(auditAll || standardFootball(competition))await expander.click();
    }
    await page.waitForTimeout(250);
    const batch=await list.evaluate(eventRows,true);
    for(const row of batch.rows)rows.set(row.key,row);
    for(const event of batch.events)events.set(event.fonbet_event_id,{...event,discovered_at:now()});
    if(targetId && events.has(targetId))return finish(false);
    const position=await list.evaluate(scrollList,false);
    if(position.top+position.height>=position.total-2){if(++stable>=4)return finish(true);}
    else stable=0;
  }
  return finish(false);
}
function centers(page){
  const frames=page.locator(FRAME+':visible').filter({has:page.locator('[class*="score-block--"]')});
  return frames.filter({hasNot:frames});
}
async function closeCenter(page){
  const frame=centers(page),count=await frame.count();
  if(!count)return;
  if(count!==1){const e=new Error('match_center_ambiguous');e.matchCenterCount=count;throw e;}
  const buttons=frame.locator('[class*="header--"] > span[class*="button--"]');
  // Verified on two real sports events: third header control closes this widget.
  if(await buttons.count()!==3)throw new Error('match_center_cleanup_unavailable');
  await buttons.last().click({timeout:3000});
  await frame.waitFor({state:'hidden',timeout:3000});
}
async function readEvent(page,event,onContext=()=>{},{reuse=false}={}){
  if(reuse)await step('close_match_center',()=>closeCenter(page));
  // Refresh the event's own context; never use an old discovery minute after queueing.
  const fresh=await step('refresh_context',()=>discover(page,event.fonbet_event_id,{reuse}));
  if(!fresh.events.length)throw new Error(fresh.complete?'event_disappeared':'discovery_incomplete');
  if(fresh.events.length!==1 || fresh.events[0].fonbet_event_id!==event.fonbet_event_id)throw new Error('event_identity_mismatch');
  event={...event,...fresh.events[0]};
  onContext({...event,observed_at:event.discovered_at});
  if(!candidate(event))throw new Error('outside_window');
  const requested_at=now();
  const row=page.locator('[class*="sport-base-event--"]').filter({has:page.locator(`a[href="${new URL(event.event_url).pathname}"]`)});
  const icon=row.locator('[resource-name^="translation_mc_soccer"]');
  if(await icon.count()!==1)throw new Error('match_center_unavailable');
  await step('open_match_center',()=>icon.click({timeout:10000}));
  const frame=centers(page);
  try{await step('wait_match_center',()=>frame.waitFor({timeout:10000}));}
  catch(error){error.matchCenterCount=await frame.count().catch(()=>0);throw error;}

  // Compact view has icon-only tabs. Expand via its verified header expand button.
  const expand=frame.locator('[class*="header--"] > span[class*="button--"]').first();
  const expanded=!(await frame.getByText('Статистика',{exact:true}).isVisible());
  if(expanded)await expand.click();
  try{
  const tab=frame.getByText('Статистика',{exact:true});
  if(await tab.count()!==1)throw new Error('statistics_unavailable');
  await step('select_statistics',()=>tab.click());
  const period=frame.getByText(/^(?:2\s*тайм|2-й тайм|Второй тайм)$/i);
  if(await period.count()!==1)throw new Error('second_half_unavailable');
  await step('select_second_half',()=>period.click());
  await page.waitForTimeout(750);
  const source_period_label=await frame.evaluate(selectedPeriod);
  if(!source_period_label)throw new Error('unconfirmed_period');
  const first=await step('read_statistics',()=>frame.evaluate(statistic));
  await page.waitForTimeout(250);
  const data=await step('read_statistics',()=>frame.evaluate(statistic));
  const confirmed=await frame.evaluate(selectedPeriod);
  const changed=['home_name','away_name','goals_home','goals_away','ht_home','ht_away','home_value','away_value'].filter(key=>first[key]!==data[key]);
  if(confirmed!==source_period_label)changed.push('period');
  if(changed.length){const error=new Error('invalid_context');error.changedFields=changed;error.collectorStage='read_statistics';throw error;}
  return normalize(event,{...data,requested_at,observed_at:now(),context_observed_at:requested_at,
    source_period_label,period_confirmed:true,minute_basis:'match_center',period_text:event.period_text});
  }finally{await closeCenter(page).catch(()=>{});if(expanded && await frame.count())await expand.click({timeout:1500}).catch(()=>{});}
}
// One retry on a fresh renderer. A retry always re-discovers the target and its clock.
async function readWithRecovery(event,{read,replace,available,record}){
  for(let attempt=0;attempt<2;attempt++){
    if(!available())throw new Error('time_budget');
    try{return await read(event);}
    catch(error){
      record({...diagnostic(error,'read_event',event.fonbet_event_id),attempt:attempt+1});
      if(attempt || !retryable(reason(error)) || !available())throw error;
      await replace();
    }
  }
}
async function collect({cdp='http://127.0.0.1:9222',maxSeconds=600,driver=chromium,
                        discoverEvents=discover,readOne=readEvent,memory=monitorMemory}={}){
  const result={run_id:randomUUID(),started_at:now(),status:'partial',summary:{reasons:{},errors:[]},observations:[]};
  const started=Date.now(),resources=memory();
  let browser,page,deadline;
  const popups=new Set();
  let crashed=false;
  const available=()=>Date.now()-started<maxSeconds*1000;
  const record=d=>{if(result.summary.errors.length<30)result.summary.errors.push(d);};
  const closePage=async()=>{
    await Promise.all([...popups].map(safeClose));
    popups.clear();
    await safeClose(page);
  };
  const replace=async()=>{
    await closePage();
    if(!available())throw new Error('time_budget');
    page=await browser.contexts()[0].newPage();crashed=false;
    page.on('crash',()=>{crashed=true;});
    page.on('popup',p=>{popups.add(p);p.on('close',()=>popups.delete(p));});
    page.setDefaultTimeout(10000);
    await page.route('**/*',r=>['image','media','font'].includes(r.request().resourceType())?r.abort():r.continue());
  };
  try{
    browser=await driver.connectOverCDP(cdp,{timeout:15000});
    deadline=setTimeout(()=>closePage().catch(()=>{}),maxSeconds*1000);deadline.unref();
    await replace();
    resources.mark?.('before_discovery');
    let discovery;
    for(let attempt=0;attempt<2;attempt++){
      try{discovery=await discoverEvents(page);break;}
      catch(error){record(diagnostic(error,'discovery'));if(attempt || !retryable(reason(error)))throw error;await replace();}
    }
    resources.mark?.('after_discovery');
    result.contexts=discovery.events.map(event=>({...event,observed_at:event.discovered_at}));
    const discoveryComplete=discovery.complete && !(discovery.link_audit?.unrecognized_rows);
    result.context_complete=discoveryComplete;result.context_scan_at=now();
    if(discovery.link_audit){result.summary.link_audit=discovery.link_audit;
      if(discovery.link_audit.unrecognized_rows)result.summary.reasons.unparsed_live_rows=discovery.link_audit.unrecognized_rows;}
    // Read the matches closest to the deadline before younger ones.
    const selected=discovery.events.filter(candidate).sort((a,b)=>clockMinute(b.clock_text)-clockMinute(a.clock_text));
    Object.assign(result.summary,{found:discovery.events.length,selected:selected.length,discovery_complete:discoveryComplete,processed:0});
    result.status=discoveryComplete?'complete':'partial';
    const updateContext=c=>{result.contexts=result.contexts.filter(old=>old.fonbet_event_id!==c.fonbet_event_id);result.contexts.push(c);};
    for(const event of selected){
      resources.mark?.('before_event',event.fonbet_event_id);
      let observation={...event,run_id:result.run_id,observation_id:randomUUID(),observed_at:now()};
      try{
        if(crashed || page.isClosed())await replace();
        const value=await readWithRecovery(event,{read:e=>readOne(page,e,updateContext,{reuse:true}),
          replace,available,record});
        observation={...observation,...value,status:'ok'};
      }catch(error){
        observation.observed_at=now();observation.status=reason(error);observation.reason=observation.status;
        result.summary.reasons[observation.status]=(result.summary.reasons[observation.status]||0)+1;
        if(retryable(observation.status) || ['invalid_context','invalid_teams','discovery_incomplete','event_identity_mismatch','time_budget'].includes(observation.status))result.status='partial';
        // A bad page is never passed to the next match, even after an exhausted retry.
        if(retryable(observation.status)){
          await closePage();
          if(browser.isConnected() && available())await replace();
        }
      }
      await Promise.all([...popups].map(safeClose));popups.clear();
      resources.mark?.('after_event',event.fonbet_event_id);
      result.observations.push(observation);result.summary.processed++;
      if(observation.status==='ok')updateContext({...observation,observed_at:observation.context_observed_at});
      if(!available() || !browser.isConnected()){
        result.summary.reasons[available()?'browser_unavailable':'time_budget_remaining']=selected.length-result.summary.processed;
        result.status='partial';break;
      }
    }
  }catch(error){
    result.status=result.observations.length?'partial':'failed';result.summary.error=reason(error);record(diagnostic(error,'collect'));
  }finally{
    clearTimeout(deadline);await closePage();
    await safeClose(browser);
    resources.mark?.('after_close');
    result.summary.memory=resources.stop();
  }
  result.finished_at=now();
  result.summary.nonempty=result.observations.filter(o=>o.status==='ok').length;
  result.summary.partial=result.observations.filter(o=>o.status==='ok'&&o.quality_status==='partial').length;
  if(result.summary.memory.oom_kill_delta>0)result.status='partial';
  return result;
}
if(require.main===module){
  const output=process.argv[2];
  if(!output){console.error('Usage: node collect.cjs OUTPUT_JSON');process.exit(2);}
  collect().then(result=>{
    fs.writeFileSync(output+'.tmp',JSON.stringify(result,null,2),{mode:0o600});fs.renameSync(output+'.tmp',output);
    console.log(JSON.stringify({run_id:result.run_id,status:result.status,summary:result.summary}));
    if(result.status==='failed')process.exitCode=1;
    // A broken CDP socket must not keep docker exec and its lock alive indefinitely.
    setTimeout(()=>process.exit(process.exitCode||0),1000).unref();
  }).catch(()=>{console.error('browser_connection_failed');process.exitCode=1;});
}
module.exports={summarizeLinks,collect,discover,readEvent,readWithRecovery,reason,centers,closeCenter};
