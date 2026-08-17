// deadveilprobe — the dead veil (2026-08-16). A session that died or vanished
// from the registry used to leave the tty view a silent black void ("long dead
// tty view"): the only tell was tiny meta-line text. The veil is a big
// splash-style island — "ended" (dead but listed) / "gone" (cid unknown) —
// re-judged on every sessions frame; a tap parks it for that cid until the
// next deliberate entry.
//
// Safe: lands on the sessions rung (#/p/self) — subscribes to nothing, claims
// no PTY size — and drives checkViewedGone()/handleJson() against a FAKE
// session pushed into sessionList, with inSessionView() stubbed so no real
// view (and no resize claim) is ever opened. Nothing is sent.
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = '/Users/clawd/clawd-harness';

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
await page.waitForFunction(() => typeof checkViewedGone === 'function').catch(() => {});
await page.waitForTimeout(600);

const r = await page.evaluate(() => {
  const CID = '00000000-probe-dead-veil';
  sessionList.push({ cid: CID, pid: 'self', title: 'probe dead session', desc: 'a fake dead session',
                     promptCount: 3, alive: false, busy: false,
                     lastActive: Date.now() / 1000, promptedAt: Date.now() / 1000 });
  const origISV = inSessionView;
  inSessionView = () => true;           // pretend we're on the tty without opening it (no resize claim)
  currentCid = CID;
  const veil = document.getElementById('deadveil');
  const up = () => !veil.hidden;
  const word = () => { const w = veil.querySelector('.dv-word'); return w ? w.textContent : ''; };
  const out = {};

  // 1) sessions-frame judgment: dead-but-listed → big "ended", splash suppressed
  checkViewedGone();
  out.deadShows = up() && word() === 'ended';
  out.titleShown = (veil.querySelector('.dv-title') || {}).textContent === 'probe dead session';
  maybeSplash(CID);
  out.noSplashOnDead = document.getElementById('splash').hidden;

  // 2) tap parks it for this cid — the next frames must not re-raise it
  veil.onclick();
  checkViewedGone();
  out.dismissSticks = !up();

  // 3) a fresh deliberate entry re-arms it (subscribe() resets the dismissal)
  deadVeilDismissed = null;
  checkViewedGone();
  out.rearms = up();

  // 4) the cid vanishing from the roster (registry pop) flips the word to "gone"
  sessionList.splice(sessionList.findIndex(s => s.cid === CID), 1);
  checkViewedGone();
  out.goneShows = up() && word() === 'gone';

  // 5) the exit frame raises it even between sessions frames
  hideDeadVeil();
  sessionList.push({ cid: CID, pid: 'self', title: 'probe dead session', alive: false });
  handleJson({ type: 'exit', cid: CID });
  out.exitShows = up() && word() === 'ended';

  // 6) leaving the session hides it
  sessionList.splice(sessionList.findIndex(s => s.cid === CID), 1);
  currentCid = null;
  checkViewedGone();
  out.hiddenOffSession = !up();

  inSessionView = origISV;
  return out;
});
console.log('DEADVEIL:', JSON.stringify(r));

const ok = Object.values(r).every(Boolean);
console.log(ok ? 'PASS — dead/gone sessions veil big and obvious, tap parks it, exit frame raises it'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
