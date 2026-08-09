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
//   2. it stays pinned there when the strip is scrolled (position:sticky, so the
//      tabs pass underneath it instead of carrying it off-screen);
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

// ---- 1 + 2: present, far right, and pinned there while the strip scrolls ----
const GEOM_FN = async () => {
  const bar = document.getElementById('sessionbar');
  const f = bar && bar.querySelector('.tfilter');
  if (!bar || bar.hidden || !f) return { ok: false, barHidden: !bar || bar.hidden, has: !!f };
  const settle = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const gapAt = () => bar.getBoundingClientRect().right - f.getBoundingClientRect().right;
  const atLeft = gapAt();
  const scrollable = bar.scrollWidth - bar.clientWidth;
  bar.scrollLeft = scrollable;                      // shove the strip to its far end
  await settle();
  const atRight = gapAt();
  bar.scrollLeft = 0; await settle();
  return { ok: true, tabs: bar.querySelectorAll('.stab').length, scrollable, atLeft, atRight,
           sticky: getComputedStyle(f).position };
};
function checkGeom(g, where) {
  console.log('GEOM' + where, JSON.stringify(g));
  if (!g.ok) { fail(`no .tfilter in the tab strip (barHidden=${g.barHidden}, has=${g.has})`); return; }
  if (g.atLeft > 16) fail(`${where}: filter is not at the far right of the strip (${g.atLeft.toFixed(1)}px gap)`);
  else pass(`${where}: filter box sits at the far right of the tab strip`);
  if (g.sticky !== 'sticky') fail(`expected position:sticky, got ${g.sticky}`);
  if (g.scrollable > 20 && Math.abs(g.atRight - g.atLeft) > 2)
    fail(`${where}: filter drifted when the strip scrolled (${g.atLeft.toFixed(1)} → ${g.atRight.toFixed(1)})`);
  else pass(g.scrollable > 20 ? `${where}: filter stays pinned while the tabs scroll under it`
                              : `${where}: strip does not overflow — pin-on-scroll not exercised`);
}
const geom = await page.evaluate(GEOM_FN);
checkGeom(geom, ' desktop');

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
