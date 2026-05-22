const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const VIEWS_DIR = path.join(__dirname, 'views');
const EXEC = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';

const variants = [
  { name: 'desktop-light', viewport: { width: 1440, height: 900 }, colorScheme: 'light', isMobile: false },
  { name: 'desktop-dark',  viewport: { width: 1440, height: 900 }, colorScheme: 'dark',  isMobile: false },
  { name: 'mobile-light',  viewport: { width: 390,  height: 844 }, colorScheme: 'light', isMobile: true,  deviceScaleFactor: 2 },
  { name: 'mobile-dark',   viewport: { width: 390,  height: 844 }, colorScheme: 'dark',  isMobile: true,  deviceScaleFactor: 2 },
];

(async () => {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  for (let n = 1; n <= 12; n++) {
    const file = path.join(VIEWS_DIR, `view-${n}.html`);
    if (!fs.existsSync(file)) { console.warn('skip', file); continue; }
    const url = 'file://' + file;
    for (const v of variants) {
      const ctx = await browser.newContext({
        viewport: v.viewport,
        colorScheme: v.colorScheme,
        deviceScaleFactor: v.deviceScaleFactor || 1,
        isMobile: v.isMobile,
        hasTouch: v.isMobile,
      });
      const page = await ctx.newPage();
      await page.emulateMedia({ colorScheme: v.colorScheme });
      await page.goto(url, { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts ? document.fonts.ready : null);
      await page.waitForTimeout(300);
      const out = path.join(VIEWS_DIR, `view-${n}-${v.name}.png`);
      await page.screenshot({ path: out, fullPage: true });
      console.log('wrote', out);
      await ctx.close();
    }
  }
  await browser.close();
})();
