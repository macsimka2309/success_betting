const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readWithRecovery,collect,reason}=require('./collect.cjs');
const {snapshot,monitorMemory}=require('./memory.cjs');
const event={fonbet_event_id:'123',home_name:'Home',away_name:'Away',competition_name:'League',clock_text:'70:12'};
test('crash retries once on new page and records safe diagnostic',async()=>{
 let attempts=0,replaced=0;const errors=[];
 const value=await readWithRecovery(event,{read:async()=>{if(++attempts===1)throw new Error('Page crashed secret=https://example/?token=x');return 7;},replace:async()=>replaced++,available:()=>true,record:e=>errors.push(e)});
 assert.equal(value,7);assert.equal(attempts,2);assert.equal(replaced,1);assert.equal(errors[0].code,'page_crashed');assert(!JSON.stringify(errors).includes('secret'));
});
test('retry is bounded and never substitutes another match',async()=>{
 let attempts=0,replaced=0;
 await assert.rejects(readWithRecovery(event,{read:async e=>{assert.equal(e.fonbet_event_id,'123');attempts++;throw new Error('Page crashed');},replace:async()=>replaced++,available:()=>true,record:()=>{}}),/crashed/);
 assert.equal(attempts,2);assert.equal(replaced,1);
});
test('no retry for disappeared target, stale clock or changed identity',async()=>{
 for(const code of ['event_disappeared','outside_window','event_identity_mismatch','discovery_incomplete']){
 let replaced=0;await assert.rejects(readWithRecovery(event,{read:async()=>{throw new Error(code);},replace:async()=>replaced++,available:()=>true,record:()=>{}}),new RegExp(code));assert.equal(replaced,0);
 }
});
test('deadline blocks a fresh attempt',async()=>{
 let allowed=true,replaced=0;
 await assert.rejects(readWithRecovery(event,{read:async()=>{allowed=false;throw new Error('Page crashed');},replace:async()=>replaced++,available:()=>allowed,record:()=>{}}),/crashed/);assert.equal(replaced,0);
});
test('collection closes failed renderer and all new pages, leaves existing browser pages alone',async()=>{
 const pages=[];let reads=0,closedBrowser=0;
 const context={newPage:async()=>{const p={closed:false,on(){},setDefaultTimeout(){},route:async()=>{},isClosed(){return this.closed;},close:async()=>{p.closed=true;}};pages.push(p);return p;}};
 const b={contexts:()=>[context],isConnected:()=>true,close:async()=>closedBrowser++};
 const result=await collect({driver:{connectOverCDP:async()=>b},discoverEvents:async()=>({events:[event,{...event,fonbet_event_id:'456'}],complete:true}),
 readOne:async(p,e)=>{assert(!p.closed);if(++reads<=2)throw new Error('Page crashed');return {...e,context_observed_at:new Date().toISOString()};},memory:()=>({stop:()=>({oom_kill_delta:0})})});
 assert.equal(result.status,'partial');assert.equal(result.observations.length,2);assert.equal(result.observations[1].status,'ok');assert.equal(reads,3);assert(pages.every(p=>p.closed));assert.equal(closedBrowser,1);
});
test('connection failure exports safe failure and releases memory sampler',async()=>{
 let stopped=false;
 const r=await collect({driver:{connectOverCDP:async()=>{throw new Error('Connection closed token=secret');}},memory:()=>({stop:()=>{stopped=true;return {oom_kill_delta:0};}})});
 assert.equal(r.status,'failed');assert(stopped);assert(!JSON.stringify(r).includes('secret'));
});
test('cgroup snapshots and OOM delta are numeric, missing metrics do not invent zero',()=>{
 const s=snapshot(p=>p.endsWith('memory.events')?'oom 2\noom_kill 1\n':p.endsWith('/stat')?'1 (node main) '+['S',...Array(18).fill('0'),'1234'].join(' '):'2048');
 assert.equal(s.oom_kill,1);assert.equal(s.generation,'1234');assert.equal(s.current_bytes,2048);
 const missing=snapshot(()=>{throw new Error('absent');});assert.equal(missing.oom_kill,null);
 let n=0;const m=monitorMemory(()=>({current_bytes:++n*10,oom_kill:n,generation:'same'}));const result=m.stop();assert.equal(result.oom_kill_delta,1);assert.equal(result.sampled_peak_bytes,20);
});

test('clock leaving window during recovery stops without another retry',async()=>{
 let count=0,replaced=0;
 await assert.rejects(readWithRecovery(event,{read:async()=>{throw new Error(++count===1?'Page crashed':'outside_window');},replace:async()=>replaced++,available:()=>true,record:()=>{}}),/outside_window/);
 assert.equal(count,2);assert.equal(replaced,1);
});

test('classifies browser failures without exporting messages or secrets',async()=>{
 for(const [message,expected] of [['strict mode violation: locator secret','selector_ambiguous'],['Element is not attached','element_detached'],['Execution context was destroyed','navigation_interrupted']])assert.equal(reason(new Error(message)),expected);
 let n=0;const errors=[];
 const r=await readWithRecovery(event,{read:async()=>{if(!n++){const e=new Error('invalid_context');e.changedFields=['home_value','password'];throw e;}return 4;},replace:async()=>{},available:()=>true,record:d=>errors.push(d)});
 assert.equal(r,4);assert.deepEqual(errors[0].changed_fields,['home_value']);assert(!JSON.stringify(errors).includes('password'));
});
test('arbitrary exception text is not a semantic code',()=>{
 assert.equal(reason(new Error('potential_secret_value')),'browser_error');
});

test('collection reads eligible matches by descending minute with stable ties',async()=>{
 const context={newPage:async()=>({on(){},setDefaultTimeout(){},route:async()=>{},isClosed:()=>false,close:async()=>{}})};
 const browser={contexts:()=>[context],isConnected:()=>true,close:async()=>{}};
 const clocks=['65:10','75:01','64:59','74:59','75:50','76:00','70:20'];
 const events=clocks.map((clock_text,i)=>({...event,fonbet_event_id:String(i+1),clock_text}));
 const order=[];
 const result=await collect({driver:{connectOverCDP:async()=>browser},
  discoverEvents:async()=>({events,complete:true}),
  readOne:async(page,e)=>{order.push(e.fonbet_event_id);return {...e,context_observed_at:new Date().toISOString()};},
  memory:()=>({stop:()=>({oom_kill_delta:0})})});
 assert.equal(result.status,'complete');
 assert.deepEqual(order,['2','5','4','7','1']);
 assert.deepEqual(result.observations.map(o=>o.fonbet_event_id),order);
 assert.deepEqual(events.map(e=>e.clock_text),clocks);
});

test('link audit deduplicates virtual rows and reports route formats',()=>{
 const {summarizeLinks}=require('./collect.cjs');
 const known={key:'event:1',path:'/live/football/2/1',format:'short',issue:null};
 const other={key:'event:2',path:'/live/football/country/cz/2/2',format:'country',issue:null};
 const bad={key:'unknown:3',path:'/live/football/new/2/3',format:'unknown',issue:'unsupported_event_url'};
 const audit=summarizeLinks([known,known,other,bad,bad]);
 assert.equal(audit.rows_seen,3);assert.equal(audit.recognized_rows,2);assert.equal(audit.unrecognized_rows,1);
 assert.deepEqual(audit.formats,{short:1,country:1,unknown:1});assert.equal(audit.samples.length,1);
});

test('unparsed routes mark collection partial while known statistics are retained',async()=>{
 const context={newPage:async()=>({on(){},setDefaultTimeout(){},route:async()=>{},isClosed:()=>false,close:async()=>{}})};
 const b={contexts:()=>[context],isConnected:()=>true,close:async()=>{}};
 const result=await collect({driver:{connectOverCDP:async()=>b},
  discoverEvents:async()=>({events:[event],complete:true,link_audit:{rows_seen:2,recognized_rows:1,unrecognized_rows:1}}),
  readOne:async(page,e)=>({...e,context_observed_at:new Date().toISOString()}),
  memory:()=>({stop:()=>({oom_kill_delta:0})})});
 assert.equal(result.status,'partial');assert.equal(result.summary.reasons.unparsed_live_rows,1);
 assert.equal(result.context_complete,false);assert.equal(result.summary.discovery_complete,false);
 assert.equal(result.observations[0].status,'ok');assert.equal(result.summary.nonempty,1);
});

test('known excluded routes are counted without an incomplete coverage warning',()=>{
 const {summarizeLinks}=require('./collect.cjs');
 const result=summarizeLinks([{key:'event:1',path:'/live/football/category/cyberfootball/2/1',format:'category_cyberfootball',excluded_route:true,issue:null}]);
 assert.equal(result.rows_seen,1);assert.equal(result.unrecognized_rows,0);assert.equal(result.excluded_route_rows,1);
});
