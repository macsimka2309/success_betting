const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {readTimeline}=require('./timeline.cjs');
test('verified sports timeline selector excludes enclosing score-timeline',async()=>{
 const b=await chromium.launch({headless:true});
 try{
  const page=await b.newPage();
  await page.setContent(fs.readFileSync(path.join(__dirname,'../../tests/fixtures/fonbet/timeline-seven-goals.html'),'utf8'));
  assert.equal(await page.locator('[class*="timeline--"][class*="scroll--"]').count(),1);
  const timeline=page.locator('[class*="timeline--"][class*="scroll--"]');
  assert.equal(await timeline.locator('[class*="row-period--"]').count(),2);
  assert.match(await timeline.innerText(),/Пенальти забит/);
 }finally{await b.close();}
});
test('reader rejects external, query-bearing and non-football URLs before browser access',async()=>{
 for(const url of ['https://evil.example/live/football/1/2','https://fon.bet/account','https://fon.bet/live/football/1/2?x=1'])
  await assert.rejects(readTimeline(url),/invalid_event_url/);
});
test('real completed results evidence has selected filter and identity link',async()=>{
 const b=await chromium.launch({headless:true});
 try{
  const page=await b.newPage();
  await page.setContent(fs.readFileSync(path.join(__dirname,'../../tests/fixtures/fonbet/completed-result.html'),'utf8'));
  assert.equal(await page.locator('[class*="filter-component-sport-mode__item--"][class*="_selected--"]').innerText(),'Завершенные');
  assert.equal(await page.locator('[class*="results-event--"] a[href$="/69626793"]').count(),1);
 }finally{await b.close();}
});

test('completion reader exports verified evidence from actual result markup',async()=>{
 const {completedResult}=require('./timeline.cjs');
 const b=await chromium.launch({headless:true});
 try{
  const page=await b.newPage();
  const html=fs.readFileSync(path.join(__dirname,'../../tests/fixtures/fonbet/completed-result.html'),'utf8');
  await page.route('**/*',route=>route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:html}));
  const data=await completedResult(page,'69626793',null);
  assert.match(data.completion_html,/69626793/);
  assert.match(data.completion_html,/_selected--/);
 }finally{await b.close();}
});

test('reads the identified late card directly and retains stoppage before querying completed results',async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext();
  const html=fs.readFileSync(path.join(__dirname,'../../tests/fixtures/fonbet/timeline-seven-goals.html'),'utf8')
    .replace('83:42','90+12').replace("84'","90+12'");
  const pathname='/live/football/country/czechrepublic/20285/69891885';
  const event='<div class="sport-base-event--test"><a href="'+pathname+'">Home — Away</a>'
   +'<div class="event-block-current-time__time--test">90+12</div><span class="period--test">2-й тайм</span></div>';
  const paths=[];
  await context.route('**/*',route=>{
   paths.push(new URL(route.request().url()).pathname);
   return route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:event+html});
  });
  const data=await readTimeline('https://fon.bet'+pathname,null,'live',{
   connect:async()=>browser,memorySnapshot:()=>({current_bytes:null,limit_bytes:null}),
  });
  assert.deepEqual(paths,[pathname]);
  assert.equal(data.source_event_id,'69891885');
  assert.equal(data.clock_text,'90+12');
  assert.equal(data.period_text,'2-й тайм');
  assert.match(data.html,/90\+12/);
 }finally{await browser.close();}
});

test('opens the matched archive card after the live event disappears',async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext();
  const live='/live/football/11978/69626793';
  const archive='/results/football/11978/69626793';
  const file=name=>fs.readFileSync(path.join(__dirname,'../../tests/fixtures/fonbet',name),'utf8');
  const paths=[];
  await context.route('**/*',route=>{
   const path=new URL(route.request().url()).pathname;
   paths.push(path);
   const body=path===live?"<script>history.replaceState({}, '', '/live/football')</script>":
    path===archive?file('timeline-seven-goals.html'):file('completed-result.html');
   return route.fulfill({status:200,contentType:'text/html; charset=utf-8',body});
  });
  const data=await readTimeline('https://fon.bet'+live,null,'result',{
   connect:async()=>browser,memorySnapshot:()=>({current_bytes:null,limit_bytes:null}),
  });
  assert.deepEqual(paths,[live,'/results/football',archive]);
  assert.match(data.completion_html,/_selected--/);
  assert.match(data.html,/Пенальти забит/);
  assert.equal(data.source_event_id,'69626793');
 }finally{await browser.close();}
});
