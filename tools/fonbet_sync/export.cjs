// Reads rendered history only. No cookies, credentials or private APIs are inspected.
const { chromium } = require('playwright');
const fs = require('node:fs');
const dates = new Set(JSON.parse(process.argv[2])); // DD.MM, Moscow placement dates
const output = process.argv[3];
const deadline = setTimeout(() => { console.error('History scan timed out'); process.exit(124); }, 240000);
deadline.unref();
(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
  let page,owned=false,success=false;
  try {
    const context=browser.contexts()[0],base=context.pages()[0];
    if(base && (base.url()==='about:blank' || base.url().startsWith('https://fon.bet/account/history/bets')))page=base;
    else {page=await context.newPage();owned=true;}
    await page.goto('https://fon.bet/account/history/bets', {waitUntil:'domcontentloaded', timeout:60000});
    await page.locator('[class*="cellCouponNumber--"]').filter({hasText:/^\d{8,}$/}).first().waitFor({timeout:30000});
    if (!page.url().includes('/account/history/bets')) throw new Error('Authorization required');
    const viewport = page.locator('[class*="virtual-list--"]').locator('..');
    const seen = new Set(), rows = [];
    let bottomStable = 0, complete = false;
    await viewport.evaluate(e => {e.scrollTop = 0;});
    for (let step=0;step<400;step++) {
      await page.waitForTimeout(300); // allow virtualized list to render after scroll
      const visible = await page.locator('[class*="cellCouponNumber--"]').evaluateAll(cells => {
        cells = cells.filter(e=>/^\d{8,}$/.test(e.textContent.trim()));
        const list = cells[0]?.closest('[class*="virtual-list--"]');
        const markers = [...(list?.querySelectorAll('[class*="stickyDate--"]') || [])]
          .map(e=>({date:e.textContent.trim(),top:parseFloat(e.parentElement.style.top)||0})).sort((a,b)=>a.top-b.top);
        return cells.map(e=>{
          const top=parseFloat(e.parentElement.parentElement.style.top)||0;
          return {id:e.textContent.trim(),date:markers.filter(m=>m.top<=top).at(-1)?.date};
        });
      });
      for (const item of visible) {
        if (!dates.has(item.date) || seen.has(item.id)) continue;
        const coupon = page.locator('[class*="cellCouponNumber--"]').filter({hasText:item.id});
        const row = coupon.locator('..');
        const expanded = (await row.getAttribute('class')).includes('_expanded--');
        if (!expanded) await coupon.click();
        const wrapper = row.locator('..');
        await wrapper.locator('[class*="coupon-sport--"]').waitFor({timeout:15000});
        const raw = await wrapper.evaluate(e=>{
          const body=[...e.querySelectorAll('[class*="coupon-sport--"] [class*="_body--"]')];
          const bar=e.querySelector('[class*="bar--"]');
          return {legs:body.map(r=>[...r.children].map(c=>c.innerText.trim())),
            bar:bar?[...bar.children].map(c=>c.innerText.trim()):[],
            type:e.querySelector('[class*="cellDescription--"]')?.innerText.trim()};
        });
        rows.push({...item,...raw});seen.add(item.id);
        await coupon.click(); // collapse to keep scroll geometry stable
      }
      const position=await viewport.evaluate(e=>({top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight}));
      if (position.top+position.height>=position.total-2) {
        if (++bottomStable>=4) {complete=true;break;}
      } else bottomStable=0;
      await viewport.evaluate(e=>{e.scrollTop+=e.clientHeight*.75;});
    }
    if (!complete) throw new Error('History traversal limit reached; refusing partial import');
    fs.writeFileSync(output,JSON.stringify({complete,dates:[...dates],rows},null,2),{mode:0o600});
    success=true;
    console.log(JSON.stringify({complete,found:rows.length}));
  } finally {
    if(success && page){
      // Keep the persistent context alive, but release the history renderer itself.
      if(!owned)await browser.contexts()[0].newPage();
      await page.close().catch(()=>{});
    }
    await browser.close();
  }
})().catch(e=>{console.error(e.message);process.exit(1);});
