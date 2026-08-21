// splashprobe — the splash's 10-minute cooldown (2026-08-09). Flipping between
// two sessions should NOT re-run the RSVP show every hop; it only earns its ~2s
// when you've been away from that session long enough to have lost the thread.
//
// Safe: lands on the sessions rung (#/p/self) — so it subscribes to nothing and
// claims no PTY size — and drives maybeSplash() directly against a FAKE session
// pushed into sessionList. No real session is touched, nothing is sent.
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));   // the repo this probe lives in, whatever the box

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
const url = `http://127.0.0.1:8787/?t=${token}#/p/self`;
const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => typeof maybeSplash === 'function').catch(() => {});
await page.waitForTimeout(600);

const r = await page.evaluate(() => {
  const CID = '00000000-probe-splash-cooldown';
  sessionList.push({ cid: CID, pid: 'self', title: 'probe session', desc: 'a fake session for the probe',
                     promptCount: 3, alive: true, busy: false, model: 'claude-opus-5',
                     lastActive: Date.now() / 1000, promptedAt: Date.now() / 1000 });
  const up = () => !document.getElementById('splash').hidden;
  const out = {};
  maybeSplash(CID);                      out.first = up();     // never seen → flashes
  hideSplash();
  maybeSplash(CID);                      out.again = up();     // straight back → silent
  hideSplash();
  // …and after the cooldown it earns the show again (rewind the stamp, don't sleep)
  seenAt[seenKey(CID)] = Date.now() - (SPLASH_COOLDOWN_MS + 1000);
  maybeSplash(CID);                      out.afterCooldown = up();
  hideSplash();
  out.persisted = !!(JSON.parse(localStorage.getItem('cc_seen_at') || '{}'))[CID];
  sessionList.splice(sessionList.findIndex(s => s.cid === CID), 1);
  return out;
});
console.log('SPLASH:', JSON.stringify(r));

const ok = r.first && !r.again && r.afterCooldown && r.persisted;
console.log(ok ? 'PASS — splash on first visit, silent on re-entry, back after the cooldown'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
