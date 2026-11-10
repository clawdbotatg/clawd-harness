#!/usr/bin/env node
// burnerprobe — guards the 🍳 back-burner island (2026-10-01): the empty middle
// of the top bar where a tab dragged off the strip parks as just its emoji.
// Lands on the sessions rung (`#/p/self`), never on a session; focusSession is
// stubbed for the click, so nothing subscribes to or resizes a real claude.
// The burner is SERVER state since 10-06 (the `burner` frame), and this page
// shows the real roster — so hsend/hsendTo are stubbed and every frame is
// captured: nothing this probe drags may park a real session.
//
// Asserts, with REAL mouse drags (HTML5 DnD), not synthetic events:
//   1. the island sits between the brand and the icon buttons;
//   2. dragging a tab onto it sends burner on:true, removes the tab and shows
//      one more emoji-only chip;
//   3. a repaint (what every `sessions` frame does) keeps the chip NODE;
//   4. clicking the chip opens that session (focusSession);
//   5. dragging the chip back onto the strip sends burner on:false and makes
//      it a tab again.
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

// NEVER let a real burner frame reach the server: capture every frame instead.
await page.evaluate(() => {
  window.__sent = [];
  window.hsend = f => { window.__sent.push(f); return true; };
  window.hsendTo = (m, f) => { window.__sent.push(f); return true; };
});
const sent = () => page.evaluate(() => window.__sent.filter(f => f.type === 'burner').map(f => ({ cid: f.cid, on: f.on })));
const chipCids = () => page.evaluate(() => [...document.querySelectorAll('#island .bchip')].map(n => n.dataset.cid));
const parkedBefore = await chipCids();        // sessions the user really has parked

// Real HTML5 drag with the mouse: grab, nudge (dragstart arms the drop hint),
// THEN measure the target.
async function drag(srcSel, dstSel) {
  const a = await page.locator(srcSel).first().boundingBox();
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
  await page.mouse.down();
  await page.mouse.move(a.x + a.width / 2 + 8, a.y + a.height / 2 + 4, { steps: 4 });
  await page.waitForTimeout(150);
  const b = await page.locator(dstSel).boundingBox();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 8 });
  await page.waitForTimeout(100);
  await page.mouse.up();
  await page.waitForTimeout(300);
}

// ---- 1: placement ----
const g = await page.evaluate(() => {
  const r = id => document.getElementById(id).getBoundingClientRect();
  return { island: r('island'), hleft: r('hleft'), btn: r('ironsBtn'),
           empty: document.getElementById('island').classList.contains('empty') };
});
if (g.island.width > 40 && g.island.left >= g.hleft.right - 1 && g.island.right <= g.btn.left + 1 && g.empty === !parkedBefore.length)
  pass(`island sits between brand and buttons (${Math.round(g.island.width)}px wide)`);
else fail('island placement ' + JSON.stringify(g));

// ---- 2: drag a tab up ----
const cid = await page.evaluate(() => railSessions()[0].cid);
await drag('#sessionbar .stab', '#island');
const s2 = {
  sent: await sent(),
  chips: await page.evaluate(() => [...document.querySelectorAll('#island .bchip')].map(n => ({ cid: n.dataset.cid, text: n.textContent }))),
  tabs: (await tabs()).length,
};
console.log('DRAG-UP', JSON.stringify(s2));
const mine = s2.chips.find(c => c.cid === cid);
if (s2.sent.some(f => f.cid === cid && f.on === true)) pass('drop sends burner on:true for that cid'); else fail('no burner on:true frame for ' + cid);
if (mine && s2.chips.length === parkedBefore.length + 1) pass('drag up parks the tab on the island');
else fail('drag up did not park the tab');
if (s2.tabs === before.length - 1) pass('the tab left the strip'); else fail(`strip has ${s2.tabs} tabs, expected ${before.length - 1}`);
if (mine && [...new Intl.Segmenter().segment(mine.text)].length <= 3) pass(`chip is emoji only ("${mine.text}")`);
else fail('chip shows more than an emoji');

// ---- 3 + 4: repaint keeps the node; click opens ----
const chipSel = `#island .bchip[data-cid="${cid}"]`;
const s3 = await page.evaluate(sel => {
  const n = document.querySelector(sel);
  if (!n) return { same: false, got: null };
  renderSessionBar();
  const same = document.querySelector(sel) === n;
  const orig = window.focusSession; let got = null;
  window.focusSession = s => { got = s.cid; };
  try { n.click(); } finally { window.focusSession = orig; }
  return { same, got };
}, chipSel);
if (s3.same) pass('a repaint keeps the chip node'); else fail('a repaint rebuilt the chip');
if (s3.got === cid) pass('clicking the chip opens its session'); else fail('chip click opened ' + s3.got);

// ---- 5: drag it back ----
if (mine) await drag(chipSel, '#sessionbar');
const s5 = { sent: await sent(), chips: (await chipCids()).length, tabs: (await tabs()).length };
console.log('DRAG-BACK', JSON.stringify(s5));
if (s5.sent.some(f => f.cid === cid && f.on === false)) pass('drag back sends burner on:false'); else fail('no burner on:false frame');
if (s5.chips === parkedBefore.length && s5.tabs === before.length) pass('drag back makes it a tab again');
else fail('drag back did not restore the tab');

await browser.close();
process.exit(failed ? 1 : 0);
