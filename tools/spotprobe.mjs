#!/usr/bin/env node
// spotprobe — guards the two-mode spotlight launcher (2026-08-28):
//   Ctrl+Shift+Space → projects mode (Enter = open project + spawn a session)
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
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
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
  } finally {
    ironList = savedIrons;
    window.openIron = savedOpenIron;
    if (up()) closeSpotlight();
  }
  return out;
});
console.log('SPOT:', JSON.stringify(r));

const ok = Object.values(r).every(Boolean);
console.log(ok ? 'PASS — Ctrl+Space irons dive, Ctrl+Shift+Space project spawn, chords switch modes'
              : 'FAIL — ' + Object.entries(r).filter(([, v]) => !v).map(([k]) => k).join(', '));
await browser.close();
process.exit(ok ? 0 : 1);
