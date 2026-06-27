// watchdogprobe — verify the replay-watchdog self-heals a black terminal, using the
// app's OWN globals (termHasContent / armReplayWatchdog are top-level function
// declarations → real window props; `term` is a const, NOT reachable from outside,
// so we drive the buffer through the app's own subscribe/handlers).
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const PORT = process.env.HARNESS_PORT || '8787';
const cid = process.argv[2], pid = process.argv[3] || 'self';
if (!cid) { console.error('usage: node watchdogprobe.mjs <cid> [pid]'); process.exit(2); }

function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  for (const d of readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse())
    for (const arch of ['mac-arm64', 'mac-x64']) {
      const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  return null;
}
const token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim();
const url = `http://127.0.0.1:${PORT}/?t=${token}#/p/${pid}/s/${cid}/tty`;

const has = (page) => page.evaluate(() => typeof termHasContent === 'function' ? termHasContent() : null);
const domLen = (page) => page.evaluate(() => { const r = document.querySelector('.xterm-rows'); return r ? r.textContent.replace(/\s+/g, '').length : -1; });

const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
await page.waitForTimeout(3500);
console.log('globals wired  :', await page.evaluate(() => ({ termHasContent: typeof termHasContent, armReplayWatchdog: typeof armReplayWatchdog })));
console.log('healthy open   : termHasContent=%s domLen=%s', await has(page), await domLen(page));

// REGRESSION: a real reload must still render (and the watchdog must NOT spuriously
// wipe a healthy screen).
await page.reload({ waitUntil: 'networkidle', timeout: 15000 });
await page.waitForTimeout(4000);
const r1 = await has(page), d1 = await domLen(page);
console.log('after reload   : termHasContent=%s domLen=%s', r1, d1);
await page.waitForTimeout(3500);   // let the 2.8s watchdog tick pass — screen must stay put
const r2 = await has(page), d2 = await domLen(page);
console.log('post-watchdog  : termHasContent=%s domLen=%s', r2, d2);

const reloadOk = r1 === true && d1 > 0;
const stable = r2 === true && d2 > 0;
console.log(reloadOk && stable
  ? 'PASS(1/2): reload renders and the watchdog leaves a healthy screen untouched.'
  : 'FAIL(1/2): reload blank or watchdog disturbed a healthy screen.');

// HEAL PATH: trick the detector into reporting the screen lost on the watchdog's first
// check, so it runs its real term.reset()+re-subscribe — that must repaint the screen.
await page.evaluate((c) => {
  const real = window.termHasContent; let n = 0;
  window.termHasContent = () => (++n <= 1 ? false : real());   // "empty" once → watchdog acts, then truth
  window.armReplayWatchdog(c);
}, cid);
const trail = [];
for (let i = 0; i < 7; i++) { await page.waitForTimeout(800); trail.push(await domLen(page)); }
console.log('heal domLen trail:', trail.join(' → '));
const dipped = Math.min(...trail) < 50;          // term.reset() wiped it
const recovered = trail[trail.length - 1] > 0;   // re-subscribe replay brought it back
console.log(recovered
  ? `PASS(2/2): watchdog re-requested the screen and it repainted (dipped=${dipped}).`
  : 'FAIL(2/2): screen stayed black after the watchdog fired.');

await page.screenshot({ path: join(HERE, 'watchdogprobe.png') });
await browser.close();
process.exit(reloadOk && stable && recovered ? 0 : 1);
