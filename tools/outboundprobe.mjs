#!/usr/bin/env node
// outboundprobe — guards the 📑 on-the-way-out island (2026-10-02): right
// after the title + breadcrumb, sessions whose 📑 wrap is armed/closing wait
// there as just their emoji until they close themselves. Lands on the sessions
// rung (`#/p/self`), never on a session; hsend/hsendTo are STUBBED so no wrap
// or wrapCancel ever reaches a real session, focusSession is stubbed for clicks.
//
// Asserts, with REAL mouse drags (HTML5 DnD), not synthetic events:
//   1. #outbound sits inside the title cluster, right after #meta, empty;
//   2. dragging a tab onto it sends the wrap frame, removes the tab, shows one
//      emoji-only chip (and not on the 🍳 island);
//   3. a repaint keeps the chip NODE;  4. clicking the chip opens its session;
//   5. dragging the chip back onto the strip sends wrapCancel, a tab again;
//   6. the server's wrapArmed alone (no local hint) puts it there, off the rail.
//
// Usage (server must be running on :8787):  cd tools && node outboundprobe.mjs

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

// NEVER let a real wrap/wrapCancel reach the server: capture every frame instead.
await page.evaluate(() => {
  window.__sent = [];
  window.hsend = f => { window.__sent.push(f); return true; };
  window.hsendTo = (m, f) => { window.__sent.push(f); return true; };
});

// Real HTML5 drag with the mouse: grab, nudge (dragstart arms the drop hint),
// THEN measure the target — an empty #outbound only opens up once a drag is live.
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

// ---- 1: placement — inside the title cluster, right after the breadcrumb ----
const g = await page.evaluate(() => {
  const r = id => document.getElementById(id).getBoundingClientRect();
  const o = document.getElementById('outbound');
  return { out: r('outbound'), meta: r('meta'), island: r('island'),
           inLeft: o.parentElement.id === 'hleft', empty: o.classList.contains('empty') };
});
if (g.inLeft && g.empty && g.out.left >= g.meta.right - 1 && g.out.right <= g.island.left + 1)
  pass('outbound island sits right after the title + breadcrumb, empty');
else fail('outbound placement ' + JSON.stringify(g));

// ---- 2: drag a tab onto it → 📑 wrap frame, tab leaves the strip, chip appears ----
const cid = await page.evaluate(() => railSessions()[0].cid);
await drag('#sessionbar .stab', '#outbound');
const s2 = await page.evaluate(() => ({
  sent: window.__sent.map(f => ({ type: f.type, cid: f.cid, text: (f.text || '').slice(0, 20) })),
  chips: [...document.querySelectorAll('#outbound .bchip')].map(n => ({ cid: n.dataset.cid, text: n.textContent })),
  tabs: document.querySelectorAll('#sessionbar .stab').length,
  burner: document.querySelectorAll('#island .bchip').length,
}));
console.log('DRAG-UP', JSON.stringify(s2));
const w = s2.sent.find(f => f.type === 'wrap');
if (w && w.cid === cid && w.text.startsWith("We're wrapping")) pass('drop sends the 📑 wrap frame (arm + prompt) for that cid');
else fail('no wrap frame for ' + cid);
if (s2.chips.length === 1 && s2.chips[0].cid === cid) pass('the session shows on the outbound island');
else fail('outbound chips: ' + JSON.stringify(s2.chips));
if (s2.tabs === before.length - 1) pass('the tab left the strip'); else fail(`strip has ${s2.tabs} tabs, expected ${before.length - 1}`);
if (s2.chips[0] && [...new Intl.Segmenter().segment(s2.chips[0].text)].length <= 3) pass(`chip is emoji only ("${s2.chips[0].text}")`);
else fail('chip shows more than an emoji');
if (!s2.burner) pass('not on the back burner too'); else fail('also on the 🍳 island');

// ---- 3 + 4: repaint keeps the node; click opens ----
const s3 = await page.evaluate(() => {
  const n = document.querySelector('#outbound .bchip');
  renderSessionBar();
  const same = document.querySelector('#outbound .bchip') === n;
  const orig = window.focusSession; let got = null;
  window.focusSession = s => { got = s.cid; };
  try { n.click(); } finally { window.focusSession = orig; }
  return { same, got };
});
if (s3.same) pass('a repaint keeps the chip node'); else fail('a repaint rebuilt the chip');
if (s3.got === cid) pass('clicking the chip opens its session'); else fail('chip click opened ' + s3.got);

// ---- 5: drag it back → wrapCancel, a tab again ----
await drag('#outbound .bchip', '#sessionbar');
const s5 = await page.evaluate(() => ({
  sent: window.__sent.map(f => f.type + ':' + f.cid),
  chips: document.querySelectorAll('#outbound .bchip').length,
  tabs: document.querySelectorAll('#sessionbar .stab').length,
}));
console.log('DRAG-BACK', JSON.stringify(s5));
if (s5.sent.includes('wrapCancel:' + cid)) pass('drag back sends wrapCancel'); else fail('no wrapCancel frame');
if (!s5.chips && s5.tabs === before.length) pass('drag back makes it a tab again');
else fail('drag back did not restore the tab');

// ---- 6: server truth — a wrapArmed session is outbound with no local hint, and
// gone from the roster = gone from the island (that's the "disappears") ----
const s6 = await page.evaluate(cid => {
  wrapLocal.clear();
  const s = sessionList.find(x => x.cid === cid);   // allSessions() hands out copies
  const was = s.wrapArmed; s.wrapArmed = true;
  renderSessionBar();
  const armed = { chip: !!document.querySelector(`#outbound .bchip[data-cid="${cid}"]`),
                  inRail: railSessions().some(x => x.cid === cid) };
  s.wrapArmed = was; renderSessionBar();
  return armed;
}, cid);
if (s6.chip && !s6.inRail) pass('a wrapArmed frame alone puts it on the outbound island, off the rail');
else fail('server-armed session not outbound: ' + JSON.stringify(s6));

await browser.close();
process.exit(failed ? 1 : 0);
