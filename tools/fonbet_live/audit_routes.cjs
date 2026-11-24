// Sports-only inventory: same virtual traversal and parser as the live collector.
const {chromium}=require('playwright');
const fs=require('node:fs');
const {discover}=require('./collect.cjs');
async function audit(output){
 let browser,page;
 const started_at=new Date().toISOString();
 try{
  browser=await chromium.connectOverCDP('http://127.0.0.1:9222',{timeout:15000});
  page=await browser.contexts()[0].newPage();page.setDefaultTimeout(10000);
  await page.route('**/*',r=>['image','media','font'].includes(r.request().resourceType())?r.abort():r.continue());
  const result=await discover(page,null,{auditAll:true});
  const report={started_at,finished_at:new Date().toISOString(),complete:result.complete,
    traversal_complete:result.traversal_complete,link_audit:result.link_audit,
    rows:result.link_rows,events:result.events.map(({fonbet_event_id,event_url,competition_name,clock_text})=>({fonbet_event_id,event_url,competition_name,clock_text}))};
  fs.writeFileSync(output+'.tmp',JSON.stringify(report,null,2),{mode:0o600});fs.renameSync(output+'.tmp',output);
  console.log(JSON.stringify({started_at,finished_at:report.finished_at,complete:report.complete,link_audit:report.link_audit}));
 }finally{if(page)await page.close().catch(()=>{});if(browser)await browser.close();}
}
if(require.main===module){
 if(!process.argv[2]){console.error('Usage: node audit_routes.cjs OUTPUT_JSON');process.exit(2);}
 const deadline=setTimeout(()=>process.exit(124),180000);deadline.unref();
 audit(process.argv[2]).catch(()=>{console.error('route_audit_failed');process.exitCode=1;}).finally(()=>clearTimeout(deadline));
}
module.exports={audit};
