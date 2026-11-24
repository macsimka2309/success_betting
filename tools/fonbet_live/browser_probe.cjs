// Probe protocol + a blank renderer only. Never serializes account DOM or session state.
const {chromium}=require('playwright');
if(!require('node:fs').existsSync('/tmp/fonbet-browser-ready')){console.error('browser_starting');process.exit(2);}
const deadline=setTimeout(()=>process.exit(2),12000);deadline.unref();
(async()=>{
 let browser,page;
 try{
  browser=await chromium.connectOverCDP('http://127.0.0.1:9222',{timeout:5000});
  page=await browser.contexts()[0].newPage();
  if(await page.evaluate(()=>1)!==1)throw new Error('renderer_unavailable');
  console.log('browser_ok');
 }finally{
  if(page)await page.close().catch(()=>{});
  if(browser)await browser.close();
 }
})().catch(()=>{console.error('browser_unavailable');process.exitCode=1;});
