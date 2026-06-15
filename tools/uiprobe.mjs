#!/usr/bin/env node
// uiprobe — drive the *running* harness UI from a LOCAL headless Chromium and report
// real DOM state + a screenshot. This exists because the only way to truly debug a
// visual bug is to watch the app run — and the MCP/claude-in-chrome browser is REMOTE
// (a cloud Chrome on another network) so it CANNOT reach 127.0.0.1:8787. A process
// launched here (Bash → node) is on the same machine as server.py, so it can.
//
// Playwright's browsers are already cached on this machine (~/Library/Caches/ms-playwright);
// we use playwright-core (no download) and point it at the cached headless shell.
//
// Usage (server must be running on :8787):
//   cd tools && npm i            # one-time, installs playwright-core (browsers already cached)
//   node uiprobe.mjs                              # snapshot the projects rung + screenshot
//   node uiprobe.mjs --hash '#/p/self/s/<cid>/tty'  # deep-link to a session, then probe #box
//   node uiprobe.mjs --hash '#/p/<pid>/s/<cid>' --box   # + grow/shrink test on the composer
//
// --box types a tall multi-line string into #box, measures height, then clears it
// (box.value='' — the same thing a send does) and re-measures. It NEVER hits send, so
// it never delivers a real message to a live claude session. Prints {resting,tall,cleared}
// and whether the textarea grew then shrank (the field-sizing:content fix).
//
// Exit code is non-zero if a requested check fails — so it works in a verify flow.

import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const PORT = process.env.HARNESS_PORT || '8787';

// Find the newest cached playwright headless-shell binary, whatever its version/arch.
function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  if (!existsSync(cache)) return null;
  const shells = readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of shells) {
    for (const arch of ['mac-arm64', 'mac-x64']) {
      const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  }
  return null;
}

const args = process.argv.slice(2);
const hash = (args.includes('--hash') ? args[args.indexOf('--hash') + 1] : '') || '';
const wantBox = args.includes('--box');

const exec = findChromium();
if (!exec) { console.error('No cached playwright chromium found. Run: cd tools && npx playwright install chromium'); process.exit(2); }

let token = '';
try { token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim(); } catch {}
const url = `http://127.0.0.1:${PORT}/?t=${token}` + hash;

const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
let failed = false;
try {
  await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
} catch (e) {
  console.error(`Could not load ${url} — is server.py running on :${PORT}?  (${e.message})`);
  await browser.close();
  process.exit(2);
}
await page.waitForTimeout(1500);

const snap = await page.evaluate(() => {
  const box = document.getElementById('box');
  const visible = box && getComputedStyle(box).display !== 'none' && box.offsetParent !== null;
  return {
    title: document.title,
    hash: location.hash || '(none)',
    composerVisible: !!visible,
    boxFieldSizing: box ? getComputedStyle(box).fieldSizing : null,
    projects: [...document.querySelectorAll('*')]
      .filter(e => /github\.com/.test(e.textContent || '') && e.children.length === 0).length,
    bodyText: document.body.innerText.replace(/\s+/g, ' ').slice(0, 240),
  };
});
console.log('SNAPSHOT', JSON.stringify(snap, null, 2));

if (wantBox) {
  if (!snap.composerVisible) {
    console.error('--box requested but the composer (#box) is not visible. Pass --hash to deep-link into a session, e.g. --hash "#/p/self/s/<cid>/tty".');
    failed = true;
  } else {
    const r = await page.evaluate(async () => {
      const box = document.getElementById('box');
      const settle = () => new Promise(res => requestAnimationFrame(() => requestAnimationFrame(res)));
      box.focus();
      box.value = ''; box.dispatchEvent(new Event('input', { bubbles: true })); await settle();
      const resting = box.offsetHeight;
      box.value = Array.from({ length: 8 }, (_, i) => 'line ' + (i + 1)).join('\n');
      box.dispatchEvent(new Event('input', { bubbles: true })); await settle();
      const tall = box.offsetHeight;
      box.value = ''; box.dispatchEvent(new Event('input', { bubbles: true })); await settle();  // what a send does
      const cleared = box.offsetHeight;
      return { resting, tall, cleared };
    });
    const grew = r.tall > r.resting;
    const shrank = r.cleared <= r.resting + 1;
    console.log('BOX', JSON.stringify({ ...r, grew, shrank }, null, 2));
    if (!grew || !shrank) { console.error('FAIL: expected the textarea to grow when filled and shrink back when cleared.'); failed = true; }
    else console.log('PASS: textarea grows on fill and shrinks back on clear.');
  }
}

const shot = join(HERE, 'uiprobe.png');
await page.screenshot({ path: shot });
console.log('screenshot ->', shot);
await browser.close();
process.exit(failed ? 1 : 0);
