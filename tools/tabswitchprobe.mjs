// tabswitchprobe — is the session tab strip clickable WHILE the splash is up,
// and does clicking a tab mid-splash jump sessions + restart the splash?
// Read-only: clicks tabs only, never types or sends anything.
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));   // the repo this probe lives in, whatever the box
const CID_A = 'faf00d8c-be9f-4449-8ece-0f078987b5ed';   // "Fix harness flashing" (self)

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
const url = `http://127.0.0.1:8787/?t=${token}#/p/self/s/${CID_A}/tty`;
const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
await page.goto(url, { waitUntil: 'domcontentloaded' });

// wait for the splash for session A
let up = null;
for (let i = 0; i < 50 && !up; i++) {
  up = await page.evaluate(() => {
    const el = document.getElementById('splash');
    if (!el || el.hidden) return null;
    return { splashCid, currentCid };
  }).catch(() => null);
  if (!up) await page.waitForTimeout(80);
}
console.log('SPLASH-A:', JSON.stringify(up));
if (!up) { console.log('FAIL (splash never appeared)'); await browser.close(); process.exit(1); }

// while it's flashing, click a DIFFERENT session's tab in the strip
const clicked = await page.evaluate(() => {
  const el = document.getElementById('splash');
  if (!el || el.hidden) return { midSplash: false };
  const tabs = [...document.querySelectorAll('#sessionbar .stab:not(.active)')];
  if (!tabs.length) return { midSplash: true, tab: null };
  const t = tabs[0];
  const r = t.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  const reachable = t === hit || t.contains(hit);       // nothing overlays the tab
  t.click();
  return { midSplash: true, tab: t.title, reachable };
}).catch(() => null);
console.log('CLICK:', JSON.stringify(clicked));

await page.waitForTimeout(400);
const after = await page.evaluate(() => ({
  currentCid, splashCid,
  splashUp: !document.getElementById('splash').hidden,
  word: document.querySelector('#splash .rsvp')?.textContent || '',
})).catch(() => null);
console.log('AFTER:', JSON.stringify(after));

const ok = clicked && clicked.midSplash && clicked.reachable &&
           after && after.currentCid !== CID_A && after.splashCid === after.currentCid;
console.log(ok ? 'PASS — tab click mid-splash jumps sessions, splash restarts for the new one'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
