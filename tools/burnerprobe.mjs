#!/usr/bin/env node
// burnerprobe — guards the 🍳 back-burner island (2026-10-01): the empty middle
// of the top bar where a tab dragged off the strip parks as just its emoji.
// Lands on the sessions rung (`#/p/self`), never on a session; focusSession is
// stubbed for the click, so nothing subscribes to or resizes a real claude.
// The burner list lives in this throwaway browser's localStorage only.
//
// Asserts, with REAL mouse drags (HTML5 DnD), not synthetic events:
//   1. the island sits between the brand and the icon buttons, empty;
//   2. dragging a tab onto it removes the tab and shows one emoji-only chip;
//   3. a repaint (what every `sessions` frame does) keeps the chip NODE;
//   4. clicking the chip opens that session (focusSession);
//   5. dragging the chip back onto the strip makes it a tab again.
//
// Usage (server must be running on :8787):  cd tools && node burnerprobe.mjs

import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const PORT = process.env.HARNESS_PORT || '8787';

function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  if (!existsSync(cache)) return null;
  const shells = readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of shells)
    for (const arch of ['mac-arm64', 'mac-x64']) {
      const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  return null;
}

const exec = findChromium();
if (!exec) { console.error('No cached playwright chromium found. Run: cd tools && npx playwright install chromium'); process.exit(2); }

let token = '';
try { token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim(); } catch {}
const url = `http://127.0.0.1:${PORT}/?t=${token}#/p/self`;

const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 900, height: 800 } });
let failed = false;
const fail = m => { console.error('FAIL: ' + m); failed = true; };
const pass = m => console.log('PASS: ' + m);

try {
  await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
} catch (e) {
  console.error(`Could not load ${url} — is server.py running on :${PORT}?  (${e.message})`);
  await browser.close(); process.exit(2);
}
await page.waitForTimeout(2000);


const tabs = () => page.evaluate(() => [...document.querySelectorAll('#sessionbar .stab')].map(t => t.title));
const before = await tabs();
if (!before.length) { console.log('SKIP: no tabs on this harness to drag'); await browser.close(); process.exit(0); }

// ---- 1: placement ----
const g = await page.evaluate(() => {
  const r = id => document.getElementById(id).getBoundingClientRect();
  return { island: r('island'), hleft: r('hleft'), btn: r('ironsBtn'),
           empty: document.getElementById('island').classList.contains('empty') };
});
if (g.island.width > 40 && g.island.left >= g.hleft.right - 1 && g.island.right <= g.btn.left + 1 && g.empty)
  pass(`island sits between brand and buttons (${Math.round(g.island.width)}px wide), empty`);
else fail('island placement ' + JSON.stringify(g));

// ---- 2: drag a tab up ----
const cid = await page.evaluate(() => railSessions()[0].cid);
const title = before[0];
await page.dragAndDrop('#sessionbar .stab >> nth=0', '#island');
await page.waitForTimeout(300);
const s2 = await page.evaluate(() => ({
  store: JSON.parse(localStorage.getItem('cc_burner') || '[]'),
  chips: [...document.querySelectorAll('#island .bchip')].map(n => ({ cid: n.dataset.cid, text: n.textContent })),
  tabs: [...document.querySelectorAll('#sessionbar .stab')].length,
}));
console.log('DRAG-UP', JSON.stringify(s2));
if (s2.store.includes(cid) && s2.chips.length === 1 && s2.chips[0].cid === cid) pass('drag up parks the tab on the island');
else fail('drag up did not park the tab');
if (s2.tabs === before.length - 1) pass('the tab left the strip'); else fail(`strip has ${s2.tabs} tabs, expected ${before.length - 1}`);
if (s2.chips[0] && [...new Intl.Segmenter().segment(s2.chips[0].text)].length <= 3) pass(`chip is emoji only ("${s2.chips[0].text}")`);
else fail('chip shows more than an emoji');

// ---- 3 + 4: repaint keeps the node; click opens ----
const s3 = await page.evaluate(async () => {
  const n = document.querySelector('#island .bchip');
  renderSessionBar();
  const same = document.querySelector('#island .bchip') === n;
  const orig = window.focusSession; let got = null;
  window.focusSession = s => { got = s.cid; };
  try { n.click(); } finally { window.focusSession = orig; }
  return { same, got };
});
if (s3.same) pass('a repaint keeps the chip node'); else fail('a repaint rebuilt the chip');
if (s3.got === cid) pass('clicking the chip opens its session'); else fail('chip click opened ' + s3.got);

// ---- 5: drag it back ----
await page.dragAndDrop('#island .bchip', '#sessionbar');
await page.waitForTimeout(300);
const s5 = await page.evaluate(() => ({
  store: JSON.parse(localStorage.getItem('cc_burner') || '[]'),
  chips: document.querySelectorAll('#island .bchip').length,
  tabs: document.querySelectorAll('#sessionbar .stab').length,
}));
console.log('DRAG-BACK', JSON.stringify(s5));
if (!s5.store.length && !s5.chips && s5.tabs === before.length) pass('drag back makes it a tab again');
else fail('drag back did not restore the tab');

await browser.close();
process.exit(failed ? 1 : 0);
