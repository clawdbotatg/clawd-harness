#!/usr/bin/env node
// spotprobe — guards the two-mode spotlight launcher (2026-08-28):
//   Ctrl+Shift+Space → projects mode (Enter = open project + spawn a session)
//   Ctrl+Alt+Space → sessions mode (Enter = focusSession; see tabfilterprobe)
//   Ctrl+Space       → irons mode    (Enter = openIron → dive into the iron's
//                                     warmest live session, no spawn)
// Asserts: each chord opens its mode (placeholder + row shape), the chords
// SWITCH modes when the other one is already up (not just toggle-close),
// typing narrows irons by title, Enter hands the selected iron's id to
// openIron and closes the overlay, and Esc closes.
// openIron is STUBBED in-page before Enter is pressed, and the fake iron has
// no members — so nothing here ever subscribes to, focuses, or messages a real
// session (the uiprobe-binds-a-real-session trap).
// Server must be running on :8787. Run: cd tools && node spotprobe.mjs

import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  const shells = readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of shells)
    for (const arch of ['mac-arm64', 'mac-x64']) {
      const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  return null;
}

const token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim();
const url = `http://127.0.0.1:8787/?t=${token}#/`;
const browser = await chromium.launch({ executablePath: findChromium() });
// The page under test is THIS checkout's index.html (direct mode serves it
// untouched), not whatever the running server has on disk.
const serveLocal = (pg) => pg.route(u => u.pathname === '/', r => r.request().resourceType() === 'document'
  ? r.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: readFileSync(join(ROOT, 'index.html'), 'utf8') })
  : r.continue());
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
await serveLocal(page);
await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => typeof openSpotlight === 'function').catch(() => {});
await page.waitForTimeout(600);

const r = await page.evaluate(async () => {
  const out = {};
  const chord = (shift) => window.dispatchEvent(new KeyboardEvent('keydown',
    { code: 'Space', key: ' ', ctrlKey: true, shiftKey: shift, bubbles: true, cancelable: true }));
  const key = (k) => spotInput.dispatchEvent(new KeyboardEvent('keydown',
    { key: k, bubbles: true, cancelable: true }));
  const up = () => !spotEl.hidden;

  // Fake irons injected around the real list; unique title so no real iron matches.
  const savedIrons = ironList;
  const savedOpenIron = window.openIron;
  let dove = null;
  window.openIron = (id) => { dove = id; };
  ironList = [{ id: 'probeiron1', title: 'zzprobe iron', desc: '', tags: [], pids: [], keys: [] },
              { id: 'probeiron2', title: 'other effort', desc: '', tags: [], pids: [], keys: [] }];
  try {
    // Ctrl+Space → irons mode
    chord(false);
    out.ironOpens = up() && spotMode === 'iron';
    out.ironPlaceholder = spotInput.placeholder.includes('iron');
    out.ironRows = spotListEl.querySelectorAll('.spotrow').length === 2 &&
                   spotListEl.textContent.includes('🔥');
    // typing narrows by title
    spotInput.value = 'zzprobe'; spotInput.dispatchEvent(new Event('input'));
    out.narrowed = spotListEl.querySelectorAll('.spotrow').length === 1;
    // Enter → openIron with the survivor's id, overlay closed
    key('Enter');
    out.enterDives = dove === 'probeiron1' && !up();

    // Ctrl+Shift+Space → projects mode; project rows carry no 🔥 prefix
    chord(true);
    out.projOpens = up() && spotMode === 'proj' && spotInput.placeholder.includes('new session');
    // the other chord SWITCHES the open overlay, not closes it
    chord(false);
    out.switches = up() && spotMode === 'iron';
    chord(true);
    out.switchesBack = up() && spotMode === 'proj';
    // same chord again toggles closed; Esc also closes
    chord(true);
    out.toggles = !up();
    chord(false); key('Escape');
    out.escCloses = !up();
    // Ctrl+Alt+Space → sessions mode ; again toggles closed
    window.dispatchEvent(new KeyboardEvent('keydown',
      { code: 'Space', key: ' ', ctrlKey: true, altKey: true, bubbles: true, cancelable: true }));
    out.sessOpens = up() && spotMode === 'sess' && spotInput.placeholder.includes('session');
    window.dispatchEvent(new KeyboardEvent('keydown',
      { code: 'Space', key: ' ', ctrlKey: true, altKey: true, bubbles: true, cancelable: true }));
    out.sessToggles = !up();
  } finally {
    ironList = savedIrons;
    window.openIron = savedOpenIron;
    if (up()) closeSpotlight();
  }
  return out;
});

// Touch: ONE real tap on a row that is not the selected one must launch it.
// (The row's mousemove handler used to rebuild the whole list, so the tap's
// own synthetic mousemove replaced the row under the finger before the click.)
// openIron is stubbed and the irons are fake — nothing real is opened.
const phone = await browser.newPage({ viewport: { width: 390, height: 780 }, hasTouch: true, isMobile: true });
await serveLocal(phone);
await phone.goto(url, { waitUntil: 'domcontentloaded' });
await phone.waitForFunction(() => typeof openSpotlight === 'function').catch(() => {});
await phone.waitForTimeout(600);
await phone.evaluate(() => {
  window.__dove = null; window.__rebuilt = 0;
  window.openIron = (id) => { window.__dove = id; };
  ironList = [{ id: 'probeiron1', title: 'zzprobe iron', desc: '', tags: [], pids: [], keys: [] },
              { id: 'probeiron2', title: 'other effort', desc: '', tags: [], pids: [], keys: [] }];
  openSpotlight('iron');
  new MutationObserver(ms => { for (const m of ms) window.__rebuilt += m.removedNodes.length; })
    .observe(spotListEl, { childList: true });
});
const box = await phone.evaluate(() => {
  const b = spotListEl.querySelectorAll('.spotrow')[1].getBoundingClientRect();
  return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
});
await phone.touchscreen.tap(box.x, box.y);
await phone.waitForTimeout(300);
const t = await phone.evaluate(() => ({ dove: window.__dove, rebuilt: window.__rebuilt, up: !spotEl.hidden }));
r.touchOneTap = t.dove === 'probeiron2' && !t.up;
r.touchNoRebuild = t.rebuilt === 0;       // the row under the finger is never replaced
// Desktop hover still moves the highlight (by toggling .sel, not rebuilding).
r.hoverSelects = await page.evaluate(async () => {
  const saved = ironList, savedOpen = window.openIron;
  window.openIron = () => {};
  ironList = [{ id: 'probeiron1', title: 'zzprobe iron', desc: '', tags: [], pids: [], keys: [] },
              { id: 'probeiron2', title: 'other effort', desc: '', tags: [], pids: [], keys: [] }];
  try {
    openSpotlight('iron');
    const rows = [...spotListEl.querySelectorAll('.spotrow')];
    rows[1].dispatchEvent(new MouseEvent('mousemove', { bubbles: true }));
    const now = [...spotListEl.querySelectorAll('.spotrow')];
    return now[1] === rows[1] && now[1].classList.contains('sel') && !now[0].classList.contains('sel') && spotSel === 1;
  } finally { ironList = saved; window.openIron = savedOpen; closeSpotlight(); }
});
console.log('SPOT:', JSON.stringify(r), JSON.stringify(t));

const ok = Object.values(r).every(Boolean);
console.log(ok ? 'PASS — Ctrl+Space irons dive, Ctrl+Shift+Space project spawn, chords switch modes'
              : 'FAIL — ' + Object.entries(r).filter(([, v]) => !v).map(([k]) => k).join(', '));
await browser.close();
process.exit(ok ? 0 : 1);
