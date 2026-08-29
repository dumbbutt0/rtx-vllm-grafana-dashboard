const { chromium } = require('playwright');

// Renders the RTX dashboard to screenshots/*.png by logging in and
// scrolling through the whole page (Grafana lazily renders off-screen panels).
// Usage: node scripts/screenshot.js [grafana_url] [user:pass]
const BASE = process.argv[2] || 'http://localhost:3001';
const AUTH = (process.argv[3] || 'admin:admin').split(':');
const OUT_DIR = __dirname + '/../screenshots';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

  // 1. login
  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('input[name="user"]', { timeout: 20000 });
  await page.fill('input[name="user"]', AUTH[0]);
  await page.fill('input[name="password"]', AUTH[1]);
  await page.click('button[type="submit"]');
  await page.waitForTimeout(5000);

  // 2. open the dashboard (UID may vary per install; pass as 3rd arg if needed)
  const uid = process.argv[4] || '9a15914d-376f-4587-b529-a4574330c22c';
  await page.goto(`${BASE}/d/${uid}/nvidia-rtx-gpu-vllm`, { waitUntil: 'domcontentloaded', timeout: 40000 });
  await page.waitForTimeout(8000);

  // 3. scroll through to force lazy render, then capture top + bottom
  await page.screenshot({ path: OUT_DIR + '/dashboard-top.png' });

  for (let i = 0; i < 10; i++) {
    await page.mouse.wheel(0, 3000);
    await page.waitForTimeout(800);
  }
  await page.waitForTimeout(5000);
  await page.screenshot({ path: OUT_DIR + '/dashboard-bottom.png' });

  await page.screenshot({ path: OUT_DIR + '/dashboard.png', fullPage: true });
  console.log('saved to', OUT_DIR);
  await browser.close();
})().catch(e => { console.error('ERR', e.message); process.exit(1); });
