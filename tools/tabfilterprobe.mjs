#!/usr/bin/env node
// tabfilterprobe — guards the 🔎 filter box that hovers over the right edge of the
// #sessionbar tab strip (2026-08-09). Same idea as uiprobe/rungprobe: drive the
// *running* app from a LOCAL headless Chromium and read the real DOM.
//
// It lands on the SESSIONS rung (`#/p/<pid>`), never on a session — the strip is
// already rendered there, so this probe subscribes to nothing, claims no PTY size
// and cannot touch a live claude.
//
// What it asserts — the four ways this widget can be broken:
//   1. it exists and sits at the FAR RIGHT of the strip (its whole point);
//   2. the strip WRAPS (2026-09-06): it never scrolls sideways, every tab is
//      inside the bar's box, and on a narrow viewport the tabs spill onto extra
//      rows rather than off-screen. (It was a single scrolling row with the
//      filter position:sticky over the tabs — that's the regression to catch.)
//   3. typing actually narrows the strip — a nonsense word hides every tab but
//      the open one, a word from a real tab keeps that tab;
//   4. a repaint doesn't eat it. renderSessionBar() runs on every `sessions`
//      frame (a couple per tool call); if it rebuilt the box, focus and the
//      half-typed word would vanish mid-sentence. Same rule as the projects rung.
//
// Usage (server must be running on :8787):  cd tools && node tabfilterprobe.mjs

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

// ---- 1 + 2: present, far right, and the strip wraps instead of scrolling ----
const GEOM_FN = async () => {
  const bar = document.getElementById('sessionbar');
  const f = bar && bar.querySelector('.tfilter');
  if (!bar || bar.hidden || !f) return { ok: false, barHidden: !bar || bar.hidden, has: !!f };
  const settle = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  await settle();
  const br = bar.getBoundingClientRect();
  const gap = br.right - f.getBoundingClientRect().right;
  const tabs = [...bar.querySelectorAll('.stab')].filter(t => t.offsetParent !== null);
  const rects = tabs.map(t => t.getBoundingClientRect());
  const rows = new Set(rects.map(r => Math.round(r.top))).size;
  const tabsW = rects.reduce((a, r) => a + r.width, 0) + 6 * Math.max(0, rects.length - 1);
  // every tab (and the filter) must sit inside the bar's own box — nothing clipped, nothing off to the side
  const inside = rects.concat([f.getBoundingClientRect()]).every(r =>
    r.left >= br.left - 1 && r.right <= br.right + 1 && r.top >= br.top - 1 && r.bottom <= br.bottom + 1);
  return { ok: true, tabs: tabs.length, gap, rows, inside, tabsW, barW: bar.clientWidth,
           overflowX: bar.scrollWidth - bar.clientWidth, overflowY: bar.scrollHeight - bar.clientHeight,
           wrap: getComputedStyle(bar).flexWrap, position: getComputedStyle(f).position };
};
function checkGeom(g, where) {
  console.log('GEOM' + where, JSON.stringify(g));
  if (!g.ok) { fail(`no .tfilter in the tab strip (barHidden=${g.barHidden}, has=${g.has})`); return; }
  if (g.gap > 16) fail(`${where}: filter is not at the far right of the strip (${g.gap.toFixed(1)}px gap)`);
  else pass(`${where}: filter box sits at the far right of the tab strip`);
  if (g.wrap !== 'wrap') fail(`${where}: #sessionbar must flex-wrap:wrap, got ${g.wrap}`);
  if (g.position === 'sticky') fail(`${where}: the filter is position:sticky again — that was the scrolling strip`);
  if (g.overflowX > 1 || g.overflowY > 1) fail(`${where}: the strip overflows its box (x=${g.overflowX}, y=${g.overflowY}) — tabs are hidden`);
  else if (!g.inside) fail(`${where}: a tab or the filter lies outside the strip's box`);
  else pass(`${where}: every tab is inside the strip — nothing to scroll to`);
  // tabs wider than the bar MUST have wrapped onto more than one row
  if (g.tabsW > g.barW) {
    if (g.rows < 2) fail(`${where}: ${g.tabs} tabs (${g.tabsW}px) outrun a ${g.barW}px strip but sit on one row`);
    else pass(`${where}: ${g.tabs} tabs wrap onto ${g.rows} rows, all visible`);
  } else pass(`${where}: ${g.tabs} tabs fit on one row — wrap not exercised at this width`);
}
const geom = await page.evaluate(GEOM_FN);
checkGeom(geom, ' desktop');
// A phone-width viewport forces the wrap with any handful of tabs.
if (geom.ok && geom.tabs >= 3) {
  await page.setViewportSize({ width: 420, height: 800 });
  await page.waitForTimeout(300);
  const narrow = await page.evaluate(GEOM_FN);
  checkGeom(narrow, ' narrow');
  if (narrow.ok && narrow.rows < 2) fail(`narrow: ${narrow.tabs} tabs at 420px did not wrap (${narrow.rows} row)`);
  await page.setViewportSize({ width: 900, height: 800 });
  await page.waitForTimeout(300);
}

// ---- 3: typing narrows the strip -------------------------------------------
if (geom.ok && geom.tabs > 0) {
  const r = await page.evaluate(async () => {
    const bar = document.getElementById('sessionbar');
    const inp = bar.querySelector('.tfilter input');
    const settle = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const vis = () => [...bar.querySelectorAll('.stab')].filter(t => t.offsetParent !== null);
    const type = async v => { inp.value = v; inp.dispatchEvent(new Event('input', { bubbles: true })); await settle(); };
    const all = vis().length;
    // a word lifted off a real tab must keep that tab
    const word = (vis().find(t => (t.querySelector('.lbl') || {}).textContent)
                  ?.querySelector('.lbl').textContent || '').split(/\s+/).filter(w => w.length > 3)[0] || '';
    await type('zzqqxx-no-such-session');
    const none = vis().length;
    const noneCls = bar.querySelector('.tfilter').classList.contains('none');
    let hit = null;
    if (word) { await type(word); hit = vis().some(t => (t.querySelector('.lbl') || {}).textContent === undefined
                                                        || t.textContent.toLowerCase().includes(word.toLowerCase())); }
    await type('');
    const back = vis().length;
    return { all, none, back, word, hit, noneCls, active: !!bar.querySelector('.stab.active') };
  });
  console.log('FILTER', JSON.stringify(r));
  // a nonsense word leaves at most the open session (which is never filtered away)
  if (r.none > (r.active ? 1 : 0)) fail(`nonsense filter left ${r.none} tabs visible`);
  else pass('a non-matching word empties the strip (bar the open session)');
  if (r.word && !r.hit) fail(`filtering by "${r.word}" hid the tab it came from`);
  else if (r.word) pass(`filtering by "${r.word}" keeps its own tab`);
  if (r.back !== r.all) fail(`clearing the filter restored ${r.back}/${r.all} tabs`);
  else pass('clearing the filter restores every tab');
}

// ---- 4: a repaint must not eat the box, its focus, or the half-typed word ---
if (geom.ok) {
  const r = await page.evaluate(async () => {
    const bar = document.getElementById('sessionbar');
    const inp = bar.querySelector('.tfilter input');
    const settle = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    inp.focus();
    inp.value = 'harn';                       // typed, but the `input` event hasn't fired yet
    renderSessionBar();                       // literally what a `sessions` frame does
    await settle();
    const now = bar.querySelector('.tfilter input');
    return { sameNode: now === inp, focused: document.activeElement === now,
             value: now ? now.value : null, lastChild: bar.lastElementChild === now.closest('.tfilter') };
  });
  console.log('REPAINT', JSON.stringify(r));
  if (!r.sameNode) fail('the repaint replaced the filter <input> node');
  else if (!r.focused) fail('the repaint stole focus from the filter box');
  else if (r.value !== 'harn') fail(`the repaint dropped un-mirrored text (value=${JSON.stringify(r.value)})`);
  else pass('a repaint keeps the box, its focus and its un-mirrored text');
  if (!r.lastChild) fail('the filter is not the last child of the strip (it must never be re-appended)');
}

// ---- 5: opening a session clears the filter (2026-08-11) --------------------
// Clicking a tab is the filter's endpoint — you found what you were narrowing
// toward — so the strip must come back whole. focusSession is stubbed for the
// click so the probe still never subscribes to (or resizes) a real session.
if (geom.ok && geom.tabs > 0) {
  const r = await page.evaluate(async () => {
    const bar = document.getElementById('sessionbar');
    const settle = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const vis = () => [...bar.querySelectorAll('.stab')].filter(t => t.offsetParent !== null);
    const inp = bar.querySelector('.tfilter input');
    inp.value = 'zzqqxx-no-such-session';
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    await settle();
    const during = vis().length;
    const orig = window.focusSession;
    let focused = 0;
    window.focusSession = () => { focused++; };
    try { bar.querySelector('.stab').click(); await settle(); }
    finally { window.focusSession = orig; }
    return { during, focused, value: bar.querySelector('.tfilter input').value,
             after: vis().length, total: bar.querySelectorAll('.stab').length };
  });
  console.log('CLICK-CLEARS', JSON.stringify(r));
  if (r.focused !== 1) fail(`tab click did not route through focusSession (stub saw ${r.focused})`);
  if (r.value !== '') fail(`clicking a tab left "${r.value}" in the filter box`);
  else if (r.after !== r.total) fail(`clicking a tab restored ${r.after}/${r.total} tabs`);
  else pass('clicking a tab clears the filter and restores the strip');
}

// ---- 2b: the same, squeezed to a phone — the width where the strip actually
// overflows, so this is where "sticky" earns its keep. -----------------------
await page.setViewportSize({ width: 380, height: 760 });
await page.waitForTimeout(600);
checkGeom(await page.evaluate(GEOM_FN), ' phone');
await page.screenshot({ path: join(HERE, 'tabfilterprobe-phone.png') });
await page.setViewportSize({ width: 900, height: 800 });
await page.waitForTimeout(400);

const shot = join(HERE, 'tabfilterprobe.png');
await page.screenshot({ path: shot });
console.log('screenshot ->', shot);
await browser.close();
process.exit(failed ? 1 : 0);
