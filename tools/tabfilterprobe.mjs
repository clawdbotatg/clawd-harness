#!/usr/bin/env node
// tabfilterprobe — guards the 🔎 that hovers over the right edge of the
// #sessionbar tab strip. It began as an inline filter box (2026-08-09); since
// 2026-10-01 it is just the icon, and a tap opens the spotlight in SESSIONS
// mode (same modal as Ctrl+Super+Space). Drives the *running* app from a LOCAL
// headless Chromium and reads the real DOM.
//
// It lands on the SESSIONS rung (`#/p/<pid>`), never on a session, and stubs
// focusSession before Enter — it subscribes to nothing and cannot touch a live
// claude.
//
// Asserts: the 🔎 sits at the FAR RIGHT and stays pinned while the strip
// scrolls (desktop + phone); a real click/tap opens the sessions spotlight,
// focused, listing every tab; typing narrows it and Enter opens the top match
// and closes the modal; a repaint never replaces the 🔎 node; "…N more" counts.
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

// ---- 3: the 🔎 opens the sessions spotlight; typing narrows; Enter opens ---
// A REAL mouse click on the icon (not element.click()). focusSession is
// stubbed before Enter so the probe never subscribes to a real session.
if (geom.ok && geom.tabs > 0) {
  const ico = await page.$('#sessionbar .tfilter .ficon');
  await ico.click();
  await page.waitForTimeout(150);
  const opened = await page.evaluate(() => ({ up: !spotEl.hidden, mode: spotMode,
    focused: document.activeElement === spotInput,
    rows: spotListEl.querySelectorAll('.spotrow').length,
    tabs: document.querySelectorAll('#sessionbar .stab').length }));
  console.log('OPEN', JSON.stringify(opened));
  if (!opened.up || opened.mode !== 'sess') fail('clicking 🔎 did not open the sessions spotlight');
  else if (!opened.focused) fail('the spotlight opened without focusing its input');
  else if (opened.rows !== opened.tabs) fail(`empty query lists ${opened.rows} rows for ${opened.tabs} tabs`);
  else pass('🔎 opens the sessions spotlight, focused, listing every tab');

  await page.keyboard.type('zzqqxx-no-such-session', { delay: 5 });
  const none = await page.evaluate(() => spotListEl.querySelectorAll('.spotrow').length);
  if (none) fail(`nonsense query left ${none} rows`); else pass('a non-matching word empties the list');
  await page.fill('#spotinput', '');
  const word = await page.evaluate(() => ((document.querySelector('#sessionbar .stab .lbl') || {}).textContent || '')
    .split(/\s+/).filter(w => w.length > 3)[0] || '');
  if (word) {
    await page.keyboard.type(word, { delay: 5 });
    await page.evaluate(() => { window.__fs = window.focusSession; window.__picked = null;
                                window.focusSession = s => { window.__picked = s; }; });
    const top = await page.evaluate(() => spotMatches()[0]?.cid);
    await page.keyboard.press('Enter');
    const r = await page.evaluate(() => { const p = window.__picked; window.focusSession = window.__fs;
      return { picked: p && p.cid, closed: spotEl.hidden }; });
    console.log('PICK', JSON.stringify({ word, top, ...r }));
    if (!top) fail(`typing "${word}" (from a real tab) matched nothing`);
    else if (r.picked !== top || !r.closed) fail('Enter did not open the highlighted session and close the modal');
    else pass(`typing "${word}" + Enter opens the matching session and closes the modal`);
  }
  if (await page.evaluate(() => !spotEl.hidden)) await page.keyboard.press('Escape');
}

// ---- 4: a repaint must not replace the 🔎 node, and it stays the last child --
if (geom.ok) {
  const r = await page.evaluate(async () => {
    const bar = document.getElementById('sessionbar');
    const before = bar.querySelector('.tfilter');
    renderSessionBar();                       // literally what a `sessions` frame does
    const now = bar.querySelector('.tfilter');
    return { sameNode: now === before, lastChild: bar.lastElementChild === now,
             noInput: !bar.querySelector('.tfilter input') };
  });
  console.log('REPAINT', JSON.stringify(r));
  if (!r.sameNode) fail('the repaint replaced the 🔎 node');
  else if (!r.lastChild) fail('the 🔎 is not the last child of the strip');
  else pass('a repaint keeps the 🔎 node in place');
  if (!r.noInput) fail('the strip still carries an inline filter <input>');
}

// ---- 2b: the same, squeezed to a phone — the width where the strip actually
// overflows, so this is where "sticky" earns its keep. -----------------------
await page.setViewportSize({ width: 380, height: 760 });
await page.waitForTimeout(600);
checkGeom(await page.evaluate(GEOM_FN), ' phone');
{
  await page.click('#sessionbar .tfilter .ficon');
  await page.waitForTimeout(150);
  const up = await page.evaluate(() => !spotEl.hidden && spotMode === 'sess');
  if (!up) fail('phone: tapping 🔎 did not open the sessions spotlight');
  else pass('phone: tapping 🔎 opens the sessions spotlight');
  await page.screenshot({ path: join(HERE, 'tabfilterprobe-modal.png') });
  await page.keyboard.press('Escape');
}
// ---- 6: "…N more" counts the tabs you can't see (2026-09-29) --------------
// N must equal the tabs less than half on screen; hidden when everything fits;
// it must follow a scroll; and a tap must scroll the strip.
{
  const r = await page.evaluate(async () => {
    const bar = document.getElementById('sessionbar');
    const more = bar && bar.querySelector('.tfilter .tmore');
    if (!more) return { has: false };
    const settle = () => new Promise(r => setTimeout(r, 450));
    const truth = () => {
      const br = bar.getBoundingClientRect();
      const cut = (more.hidden ? bar.querySelector('.tfilter .ficon') : more).getBoundingClientRect().left;
      return [...bar.querySelectorAll('.stab')].filter(t => {
        const q = t.getBoundingClientRect(), m = q.left + q.width / 2;
        return m > cut || m < br.left; }).length;
    };
    const shown = () => more.hidden ? 0 : parseInt(more.textContent.replace(/\D+/g, ''), 10);
    const overflow = bar.scrollWidth - bar.clientWidth > 20;
    const a = { shown: shown(), truth: truth() };
    const x0 = bar.scrollLeft; more.hidden || more.click(); await settle();
    const moved = bar.scrollLeft !== x0;
    const b = { shown: shown(), truth: truth() };
    bar.scrollLeft = 0; await settle();
    return { has: true, overflow, a, b, moved };
  });
  console.log('MORE', JSON.stringify(r));
  if (!r.has) fail('no .tmore counter next to the 🔎');
  else {
    if (r.a.shown !== r.a.truth) fail(`counter says ${r.a.shown} hidden, ${r.a.truth} actually are`);
    else pass(`counter shows ${r.a.shown} hidden tab(s), matching the strip`);
    if (r.overflow && !r.a.shown) fail('strip overflows but the counter is hidden');
    if (r.a.shown && !r.moved) fail('tapping the counter did not scroll the strip');
    if (r.b.shown !== r.b.truth) fail(`after a scroll the counter says ${r.b.shown}, truth ${r.b.truth}`);
    else if (r.a.shown) pass('counter follows a scroll; tap scrolls the strip');
  }
}
await page.screenshot({ path: join(HERE, 'tabfilterprobe-phone.png') });
await page.setViewportSize({ width: 900, height: 800 });
await page.waitForTimeout(400);

const shot = join(HERE, 'tabfilterprobe.png');
await page.screenshot({ path: shot });
console.log('screenshot ->', shot);
await browser.close();
process.exit(failed ? 1 : 0);
