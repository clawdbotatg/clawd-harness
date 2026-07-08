// Probe the 🧠 accounts overlay on a running harness (HARNESS_PORT env, default 8787):
// click the header button, assert the modal opens with account cards, screenshot.
import { chromium } from 'playwright-core';
import { readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
let exec = null;
for (const d of readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse())
  for (const a of ['mac-arm64', 'mac-x64']) {
    const b = join(cache, d, `chrome-headless-shell-${a}`, 'chrome-headless-shell');
    if (!exec && existsSync(b)) exec = b;
  }
const PORT = process.env.HARNESS_PORT || '8787';
const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'networkidle', timeout: 15000 });
await page.waitForTimeout(1500);
await page.click('#accountsBtn');
await page.waitForTimeout(800);
const state = await page.evaluate(() => ({
  modalOpen: !document.getElementById('accountsmodal').hidden,
  text: document.getElementById('accountsbody').innerText.replace(/\s+/g, ' ').slice(0, 300),
  cards: document.querySelectorAll('#accountsbody .acct').length,
}));
console.log(JSON.stringify(state, null, 2));
await page.screenshot({ path: process.env.SHOT || 'brain.png' });
await browser.close();
if (!state.modalOpen || state.cards < 1) { console.error('FAIL: overlay did not open with accounts'); process.exit(1); }
console.log('PASS');
