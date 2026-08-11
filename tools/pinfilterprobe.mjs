#!/usr/bin/env node
// pinfilterprobe — guards the 🔎 filter on the 📌 pin board (2026-08-11). Same
// idea as tabfilterprobe: drive the *running* app from a LOCAL headless
// Chromium and read the real DOM.
//
// It lands on `#/pins` — a top-level view like the PM tab — so it subscribes to
// nothing and claims no PTY size. The pins it filters are FAKES injected into
// `sessionList` (the splashprobe pattern): no real session is touched, and the
// next real `sessions` frame washes the fakes away regardless.
//
// What it asserts — the ways this widget can be broken:
//   1. the box exists in #pinhead, OUTSIDE #pinboard (the board repaints with
//      innerHTML='' on every sessions frame; a box inside it dies mid-word);
//   2. typing narrows the board — a nonsense word hides every card (+ paints
//      the red `none` state), a word from a real card's 🧪 test hint keeps
//      exactly that card, clearing restores all, and the title counts n/total;
//   3. a repaint (renderPinBoard(), literally what a frame does) keeps the
//      box, its focus, and text the `input` event hasn't mirrored yet;
//   4. Enter on a filter narrowed to one pin opens it (focusSession stubbed)
//      and clears the filter, so the board is whole on the next visit.
//
// Usage (server must be running on :8787):  cd tools && node pinfilterprobe.mjs

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
const url = `http://127.0.0.1:${PORT}/?t=${token}#/pins`;

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

// One evaluate for the whole drill: fakes in, every assertion, fakes out —
// minimizes the window in which a real `sessions` frame can repaint over them.
const r = await page.evaluate(async () => {
  const out = {};
  const settle = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const board = document.getElementById('pinboard');
  const head = document.getElementById('pinhead');
  const box = document.getElementById('pinfilter');
  const inp = box && box.querySelector('input');
  out.chrome = { head: !!head, box: !!box, inView: currentView() === 'pins',
                 outsideBoard: !!box && !board.contains(box) };
  if (!box) return out;

  const FAKE = [
    { cid: '__pf1', pid: 'self', title: 'walrus latency fix', desc: 'tail lag on the walrus feed',
      testHint: 'open the walrus dashboard and confirm p95 under 200ms', pinned: Date.now() / 1000 - 60,
      alive: true, promptCount: 3, engine: 'claude' },
    { cid: '__pf2', pid: 'self', title: 'nginx upload cap', desc: 'bump the relay body limit',
      testHint: 'upload a 5MB image through h.atg.link', pinned: Date.now() / 1000 - 120,
      alive: true, promptCount: 7, engine: 'claude' },
    { cid: '__pf3', pid: 'self', title: 'quick prompt chips', digest: 'reordered the array',
      pinned: Date.now() / 1000 - 180, alive: false, promptCount: 2, engine: 'codex' },
  ];
  const saved = sessionList;
  sessionList = FAKE;
  const origFocus = window.focusSession;
  let focusedCid = null;
  window.focusSession = s => { focusedCid = s && s.cid; };
  try {
    renderPinsUI(); await settle();
    const vis = () => [...board.querySelectorAll('.pincard')].filter(c => c.offsetParent !== null);
    const type = async v => { inp.value = v; inp.dispatchEvent(new Event('input', { bubbles: true })); await settle(); };
    out.built = { cards: board.querySelectorAll('.pincard').length, boxHidden: box.hidden,
                  title: document.getElementById('pintitle').textContent };

    // ---- 2: narrowing ----
    await type('zzqqxx-no-such-pin');
    out.none = { vis: vis().length, cls: box.classList.contains('none'),
                 title: document.getElementById('pintitle').textContent };
    await type('walrus p95');              // words ANDed, lifted off title + 🧪 hint
    out.hint = { vis: vis().length, cid: vis()[0] && vis()[0].dataset.cid };
    await type('');
    out.cleared = { vis: vis().length, on: box.classList.contains('on') };

    // ---- 3: repaint survival ----
    inp.focus();
    inp.value = 'wal';                     // typed, but the `input` event hasn't fired yet
    renderPinBoard();                      // literally what a `sessions` frame does
    await settle();
    const now = document.querySelector('#pinfilter input');
    out.repaint = { sameNode: now === inp, focused: document.activeElement === now,
                    value: now ? now.value : null };

    // ---- 4: Enter on a single hit opens it + clears ----
    await type('nginx');
    const single = vis().length;
    inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await settle();
    out.enter = { single, focusedCid, value: inp.value, visAfter: vis().length };
  } finally {
    window.focusSession = origFocus;
    sessionList = saved;
    setPinFilter('');
    renderPinsUI();
  }
  return out;
});

console.log(JSON.stringify(r, null, 1));
if (!r.chrome.box) { fail('no #pinfilter box in the DOM'); }
else {
  if (!r.chrome.outsideBoard) fail('the filter box is INSIDE #pinboard — the repaint will eat it');
  else pass('filter box lives outside the repainted board');
  if (r.built.cards !== 3 || r.built.boxHidden) fail(`board built ${r.built.cards}/3 fake cards (boxHidden=${r.built.boxHidden})`);
  else pass('3 fake pins render and unhide the filter box');
  if (r.none.vis !== 0 || !r.none.cls) fail(`nonsense word left ${r.none.vis} cards visible (none=${r.none.cls})`);
  else pass('a non-matching word empties the board and paints the red state');
  if (!/0\/3/.test(r.none.title)) fail(`title did not count 0/3 while filtering (${JSON.stringify(r.none.title)})`);
  else pass('the board title counts n/total while a filter is live');
  if (r.hint.vis !== 1 || r.hint.cid !== '__pf1') fail(`"walrus p95" matched ${r.hint.vis} cards (${r.hint.cid}) — 🧪 hint words must AND with the title`);
  else pass('ANDed words spanning title + 🧪 test hint keep exactly their card');
  if (r.cleared.vis !== 3 || r.cleared.on) fail(`clearing restored ${r.cleared.vis}/3 cards (on=${r.cleared.on})`);
  else pass('clearing the filter restores every card');
  if (!r.repaint.sameNode) fail('the repaint replaced the filter <input> node');
  else if (!r.repaint.focused) fail('the repaint stole focus from the filter box');
  else if (r.repaint.value !== 'wal') fail(`the repaint dropped un-mirrored text (value=${JSON.stringify(r.repaint.value)})`);
  else pass('a repaint keeps the box, its focus and its un-mirrored text');
  if (r.enter.single !== 1 || r.enter.focusedCid !== '__pf2') fail(`Enter on a single hit did not open it (single=${r.enter.single}, cid=${r.enter.focusedCid})`);
  else if (r.enter.value !== '') fail(`Enter left "${r.enter.value}" in the box`);
  else pass('Enter on a filter narrowed to one pin opens it and clears the filter');
}

const shot = join(HERE, 'pinfilterprobe.png');
await page.screenshot({ path: shot });
console.log('screenshot ->', shot);
await browser.close();
process.exit(failed ? 1 : 0);
