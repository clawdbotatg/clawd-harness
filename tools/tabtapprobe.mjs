#!/usr/bin/env node
// tabtapprobe — guards "I click a tab and nothing happens" (Austin, 2026-11-09).
// The tab strip repaints on every sessions/projects frame and roster pulse,
// several times a second. It used to delete and recreate every tab each time,
// so a click whose press landed on the old node and whose release landed on
// its replacement never fired. Tabs are now reconciled by cid (landmine 4).
//
// Lands on the sessions rung (`#/p/self`), never on a session. hsend/hsendTo
// are stubbed and focusSession is captured, so nothing subscribes to, resizes
// or types into a real session.
//
// Asserts:
//   1. a repaint keeps every tab NODE (same element, not a look-alike);
//   2. REAL mouse: press on a tab, repaints land while the button is down,
//      release → the tab's session opens;
//   3. REAL touch (second, phone-shaped page): touchstart, repaints, touchend
//      → the tab's session opens;
//   4. a changed title is written into the SAME node;
//   5. a reordered roster moves the nodes instead of rebuilding them;
//   6. a session that leaves the roster loses its tab, the rest keep theirs.
//
// Usage (server must be running on :8787):  cd tools && node tabtapprobe.mjs

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
let failed = false;
const fail = m => { console.error('FAIL: ' + m); failed = true; };
const pass = m => console.log('PASS: ' + m);
const check = (name, ok, detail) => ok ? pass(name) : fail(name + (detail ? ' — ' + detail : ''));

// A page with the wire cut: frames we'd send are captured, focusSession records.
async function open(ctxOpts) {
  const ctx = await browser.newContext(ctxOpts);
  const page = await ctx.newPage();
  try {
    await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
  } catch (e) {
    console.error(`Could not load ${url} — is server.py running on :${PORT}?  (${e.message})`);
    await browser.close(); process.exit(2);
  }
  await page.waitForTimeout(2000);
  await page.evaluate(() => {
    window.__sent = [];
    window.hsend = f => { window.__sent.push(f); return true; };
    window.hsendTo = (m, f) => { window.__sent.push(f); return true; };
    window.__opened = [];
    window.focusSession = s => { window.__opened.push(s && s.cid); };
  });
  return page;
}
const tabCids = page => page.evaluate(() => [...document.querySelectorAll('#sessionbar .stab')].map(t => t.dataset.cid));
// what a burst of server frames does to the strip
const storm = (page, n = 6) => page.evaluate(n => { for (let i = 0; i < n; i++) renderSessionBar(); }, n);

// ───────────── desktop ─────────────
const page = await open({ viewport: { width: 1200, height: 800 } });
const before = await tabCids(page);
if (before.length < 2) { console.log('SKIP: fewer than two tabs on this harness'); await browser.close(); process.exit(0); }

// ---- 1: a repaint keeps every node ----
const kept = await page.evaluate(() => {
  const nodes = [...document.querySelectorAll('#sessionbar .stab')];
  for (let i = 0; i < 5; i++) renderSessionBar();
  const now = [...document.querySelectorAll('#sessionbar .stab')];
  return { n: nodes.length, same: nodes.length === now.length && nodes.every((x, i) => x === now[i]),
           filterLast: document.getElementById('sessionbar').lastElementChild.classList.contains('tfilter') };
});
check('a repaint keeps every tab node', kept.same, JSON.stringify(kept));
check('the 🔎 box stays last on the strip', kept.filterLast);

// ---- 2: mouse press → frames land → release ----
const target = before[1];
const box = await page.locator(`#sessionbar .stab[data-cid="${target}"]`).boundingBox();
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
await page.mouse.down();
await storm(page);                       // frames arrive while the button is held
await page.waitForTimeout(40);
await storm(page);
await page.mouse.up();
await page.waitForTimeout(100);
const opened = await page.evaluate(() => window.__opened.slice());
check('mouse: a click that spans repaints opens that tab', opened.length === 1 && opened[0] === target, 'opened=' + JSON.stringify(opened));

// ---- 4: a changed title lands in the same node ----
const retitled = await page.evaluate(cid => {
  const node = document.querySelector(`#sessionbar .stab[data-cid="${cid}"]`);
  const s = sessionList.find(x => x.cid === cid);          // allSessions() hands out copies
  if (!s) return { skip: true };
  const was = { tab: s.tab, title: s.title };
  s.tab = ''; s.title = 'tabtapprobe-retitled';
  renderSessionBar();
  const now = document.querySelector(`#sessionbar .stab[data-cid="${cid}"]`);
  const out = { same: now === node, text: now.querySelector('.lbl').textContent };
  s.tab = was.tab; s.title = was.title; renderSessionBar();
  return out;
}, target);
if (retitled.skip) console.log('SKIP: title check (session not on this machine\'s list)');
else check('a new title is written into the same node', retitled.same && retitled.text.includes('tabtapprobe-retitled'), JSON.stringify(retitled));

// ---- 5 + 6: reorder moves nodes; a departed session loses only its own tab ----
const moved = await page.evaluate(() => {
  const nodes = new Map([...document.querySelectorAll('#sessionbar .stab')].map(n => [n.dataset.cid, n]));
  const orig = window.railSessions;
  const rev = () => orig().slice().reverse();
  window.railSessions = rev;
  let out;
  try {
    renderSessionBar();
    const now = [...document.querySelectorAll('#sessionbar .stab')];
    const want = rev().map(s => s.cid);
    out = { ordered: now.map(n => n.dataset.cid).join() === want.join(),
            sameNodes: now.every(n => nodes.get(n.dataset.cid) === n) };
    const gone = want[0];
    window.railSessions = () => rev().slice(1);
    renderSessionBar();
    const after = [...document.querySelectorAll('#sessionbar .stab')];
    out.dropped = !after.some(n => n.dataset.cid === gone) && after.length === want.length - 1;
    out.restKept = after.every(n => nodes.get(n.dataset.cid) === n);
  } finally { window.railSessions = orig; renderSessionBar(); }
  out.restored = [...document.querySelectorAll('#sessionbar .stab')].length === nodes.size;
  return out;
});
check('a reordered roster reorders the tabs', moved.ordered, JSON.stringify(moved));
check('…by moving the existing nodes', moved.sameNodes);
check('a session that left loses its tab', moved.dropped);
check('…and the other tabs keep their nodes', moved.restKept && moved.restored);

const sent = await page.evaluate(() => window.__sent.length);
check('nothing was sent to the server', sent === 0, 'frames=' + sent);
await page.context().close();

// ───────────── touch ─────────────
const phone = await open({ viewport: { width: 430, height: 860 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
const ptabs = await tabCids(phone);
if (ptabs.length < 1) console.log('SKIP: no tabs on the phone-shaped page');
else {
  const pt = ptabs[0];
  await phone.evaluate(cid => document.querySelector(`#sessionbar .stab[data-cid="${cid}"]`).scrollIntoView({ inline: 'center' }), pt);
  const b = await phone.locator(`#sessionbar .stab[data-cid="${pt}"]`).boundingBox();
  const x = Math.round(b.x + b.width / 2), y = Math.round(b.y + b.height / 2);
  const cdp = await phone.context().newCDPSession(phone);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
  await storm(phone);                    // frames arrive while the finger is down
  await phone.waitForTimeout(40);
  await storm(phone);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await phone.waitForTimeout(250);
  const po = await phone.evaluate(() => window.__opened.slice());
  check('touch: a tap that spans repaints opens that tab', po.length === 1 && po[0] === pt, 'opened=' + JSON.stringify(po));
  const psent = await phone.evaluate(() => window.__sent.length);
  check('touch: nothing was sent to the server', psent === 0, 'frames=' + psent);
}

await browser.close();
console.log(failed ? 'tabtapprobe: FAIL' : 'tabtapprobe: all green');
process.exit(failed ? 1 : 0);
