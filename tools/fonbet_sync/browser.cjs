const { chromium } = require('playwright');
const fs=require('node:fs');
const READY='/tmp/fonbet-browser-ready';
(async () => {
  const context = await chromium.launchPersistentContext('/profile', {
    headless: false, locale: 'ru-RU', timezoneId: 'Europe/Moscow',
    viewport: null, args: ['--remote-debugging-port=9222', '--window-size=1280,900', '--disable-dev-shm-usage']
  });
  let stopping=false;
  const shutdown=async()=>{if(stopping)return;stopping=true;fs.rmSync(READY,{force:true});await context.close().catch(()=>{});process.exit(0);};
  process.on('SIGTERM',shutdown);process.on('SIGINT',shutdown);
  const page = context.pages()[0] || await context.newPage();
  await page.goto('https://fon.bet/account/history/bets', {waitUntil: 'domcontentloaded', timeout: 60000});
  fs.writeFileSync(READY,'ready',{mode:0o600});
  console.log('Browser ready for manual sign-in');
  await new Promise(resolve => context.on('close', resolve));
})().catch(error => {console.error(error.message);process.exit(1);});
