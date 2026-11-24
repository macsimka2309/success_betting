'use strict';
// Called under the same host and container locks as the existing live collector.
const {chromium}=require('playwright');
const {centers}=require('./collect.cjs');
const {snapshot}=require('./memory.cjs');
async function completedResult(page,eventId,resultDay){
 await page.goto('https://fon.bet/results/football',{waitUntil:'domcontentloaded',timeout:12000});
 if(new URL(page.url()).origin!=='https://fon.bet')return {};
 const filter=page.getByText('Завершенные',{exact:true});await filter.waitFor({timeout:5000});
 if(!await filter.evaluate(e=>[...e.parentElement.classList].some(c=>c.startsWith('_selected--'))))await filter.click();
 const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Moscow'}).format(new Date());
 const ago=resultDay?Math.round((Date.parse(today)-Date.parse(resultDay))/86400000):0;
 if(!Number.isInteger(ago)||ago<0||ago>14)return {};
 // The signal can cross midnight relative to kickoff: inspect its day and previous day.
 for(let day=0;day<=ago+1;day++){
  if(day>0)await page.locator('[class*="dateContainer--"] [class*="arrow--"][class*="_left--"]').click({timeout:2000});
  if(day<ago)continue;
  const row=page.locator('[class*="results-event--"]').filter({has:page.locator('a[href$="/'+eventId+'"], a[href$="/'+eventId+'/"]')});
  try{await row.waitFor({timeout:1500});}catch{continue;}
  if(await row.count()===1){
   const selected=await filter.evaluate(e=>e.parentElement.outerHTML);
   const details=await row.evaluate(event=>{
    // Use only the nearest unique score header enclosing this row.
    let captions='';
    for(let group=event.parentElement;group&&group!==document.body;group=group.parentElement){
     const headers=[...group.querySelectorAll('[class*="score-caption--"]')];
     if(!headers.length)continue;
     if(headers.every(heading=>!heading.closest('[class*="results-event--"]')) &&
        headers.every(heading=>heading.parentElement===headers[0].parentElement)){
      captions=headers.map(heading=>heading.outerHTML).join('');
     }
     break;
    }
    return captions+event.outerHTML;
   });
   const prefix='/results/football/';
   const links=await row.locator('a[href^="'+prefix+'"]').evaluateAll(
    anchors=>anchors.map(anchor=>anchor.getAttribute('href')));
   const resultPath=links.find(href=>/^\/results\/football\/\d+\/\d+\/?$/.test(href)
    &&href.replace(/\/$/,'').endsWith('/'+eventId));
   return {completion_html:selected+details,result_path:resultPath};
  }
 }
 return {};
}
const terminalLabel=/окончен|заверш|дополнительн|пенальти|отмен[её]н|прерван/i;
async function readCenter(frame,eventId,clockContext,timeout){
 await frame.waitFor({timeout});
 const tab=frame.getByText('Ход матча',{exact:true});
 if(!await tab.isVisible())await frame.locator('[class*="header--"] > span[class*="button--"]').first().click({timeout});
 await tab.click({timeout});
 const timeline=frame.locator('[class*="timeline--"][class*="scroll--"]');
 await timeline.locator('[class*="row-period--"]').first().waitFor({timeout});
 const html=await frame.evaluate(e=>e.outerHTML);
 const clock=await frame.locator('[class*="scoreboard-timer__value--"]').innerText({timeout:1000}).catch(()=>'');
 return {html,source_event_id:eventId,observed_at:new Date().toISOString(),
  clock_text:/^\d/.test(clock)?clock:clockContext.clock_text||clock,
  period_text:terminalLabel.test(clock)?clock:clockContext.period_text||''};
}

async function readTimeline(url,resultDay=null,stage='live',{
 connect=()=>chromium.connectOverCDP('http://127.0.0.1:9222',{timeout:5000}), memorySnapshot=snapshot,
}={}){
 const parsed=new URL(url);
 if(parsed.origin!=='https://fon.bet'||parsed.username||parsed.password||parsed.search||parsed.hash||!/^\/live\/football\/(?:(?:country\/[A-Za-z0-9-]+|category\/(?:internationalteams|internationalclubs))\/)?\d+\/\d+\/?$/.test(parsed.pathname))throw Error('invalid_event_url');
 const eventId=parsed.pathname.split('/').filter(Boolean).at(-1);
 const memory=memorySnapshot();
 if(memory.current_bytes!==null&&memory.limit_bytes!==null&&memory.current_bytes>memory.limit_bytes*.70)return {reason:'browser_memory_budget',contact_attempted:false};
 let browser,page,timer;let completed={},clockContext={};let contacted=false;
 try{
  browser=await connect();
  page=await browser.contexts()[0].newPage();
  return await Promise.race([(async()=>{
   contacted=true;
   await page.goto(url,{waitUntil:'domcontentloaded',timeout:12000});
   const rendered=new URL(page.url()), matchPath=parsed.pathname.replace(/\/$/,'');
   const sameMatch=rendered.origin===parsed.origin&&rendered.pathname.replace(/\/$/,'')===matchPath;
   const row=page.locator('[class*="sport-base-event--"]').filter({has:page.locator('a[href="'+matchPath+'"], a[href="'+matchPath+'/"]')});
   if(sameMatch)await Promise.any([
    row.first().waitFor({timeout:5000}),
    centers(page).first().waitFor({timeout:5000}),
   ]).catch(()=>{});
   const event=sameMatch&&await row.count()===1?await row.evaluate(e=>({
    clock_text:e.querySelector('[class*="event-block-current-time__time--"]')?.textContent.trim(),
    period_text:e.querySelector('[class*="period--"]')?.textContent.trim(),
   })):null;
   const icon=row.locator('[resource-name^="translation_mc_soccer"]');
   const frame=centers(page);
   if(event)clockContext={clock_text:event.clock_text,period_text:event.period_text};
   const available=sameMatch&&(await frame.count()===1||await icon.count()===1);
   let result=null;
   if(available){
    // On this exact route a matched row can open the center, or the card
    // can already own one visible center.
    result=await (async()=>{
     if(!await frame.count())await icon.click({timeout:5000});
     return readCenter(frame,eventId,clockContext,5000);
    })().catch(()=>null);
   }
   if(!result || stage==='result' || terminalLabel.test(result.period_text)){
    completed=await completedResult(page,eventId,resultDay).catch(()=>({}));
   }
   if(!result && completed.result_path){
    result=await (async()=>{
     const archive=new URL(completed.result_path,parsed.origin);
     await page.goto(archive.href,{waitUntil:'domcontentloaded',timeout:6000});
     const opened=new URL(page.url());
     if(opened.origin!==archive.origin||opened.pathname.replace(/\/$/,'')!==archive.pathname.replace(/\/$/,''))return null;
     return readCenter(centers(page),eventId,{},2500);
    })().catch(()=>null);
   }
   return {...completed,...clockContext,contact_attempted:contacted,
    ...(result||{reason:available?'timeline_unavailable':'live_event_missing'})};
  })(),new Promise(resolve=>{timer=setTimeout(()=>resolve({...completed,...clockContext,contact_attempted:contacted,reason:'browser_time_budget'}),50000);})]);
 }catch{return {...completed,...clockContext,contact_attempted:contacted,reason:'timeline_unavailable'};}finally{clearTimeout(timer);if(page)await page.close().catch(()=>{});if(browser)await browser.close();}
}
if(require.main===module)readTimeline(process.argv[2],process.argv[3]||null,process.argv[4]).then(v=>console.log(JSON.stringify(v))).catch(()=>{console.log(JSON.stringify({reason:'timeline_unavailable',contact_attempted:false}));process.exitCode=1;});
module.exports={readTimeline,completedResult};
