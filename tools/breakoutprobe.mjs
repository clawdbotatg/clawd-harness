// breakoutprobe — ⏏ break out (2026-09-11). A session can be ejected into its
// own popup window carrying `?breakout=1`; that window hides every bit of
// harness chrome above the session pane (header, iron row, tab strip, needs
// bar) and shows just the terminal + the composer. Guards: the ⏏ shows only in
// a session view (desktop), a REAL click opens a popup whose URL carries the
// flag + this session's tty hash, and inside that window the chrome is gone,
// the ⏏ itself is gone, the pane starts at the top edge, and the window is
// titled by the session.
//
// Safe: fake session pushed into sessionList with hsend stubbed in BOTH
// windows, so nothing reaches the server and no real session is bound. The
// popup's boot resolves the fake cid to nothing (no subscribe).
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

const CID = '00000000-probe-breakout-eject';
const token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim();
const url = `http://127.0.0.1:8787/?t=${token}#/p/self`;
const browser = await chromium.launch({ executablePath: findChromium() });
const context = await browser.newContext({ viewport: { width: 1100, height: 800 } });
// stub the wire in EVERY page of this context (the popup included) before the app boots
await context.addInitScript(() => {
  window.addEventListener('DOMContentLoaded', () => {
    const arm = () => { if (typeof hsend === 'function' && !window.__stubbed) { window.__sent = []; hsend = (f) => { window.__sent.push(f); return true; }; window.__stubbed = true; } };
    arm(); setTimeout(arm, 50); setTimeout(arm, 300);
  });
});
const page = await context.newPage();
await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => typeof focusSession === 'function' && window.__stubbed).catch(() => {});
await page.waitForTimeout(600);

const fake = (cid) => ({ cid, pid: 'self', title: 'probe breakout session', desc: 'fake', tab: 'eject-me',
  promptCount: 3, alive: true, busy: false, model: 'claude-opus-5', lastActive: Date.now() / 1000, promptedAt: Date.now() / 1000 });

const r1 = await page.evaluate(({ CID, fake }) => {
  const out = {};
  const btn = document.getElementById('ejectBtn');
  // the popup's own boot will re-resolve a fake cid to the rung and rewrite its
  // hash — so record what the ⏏ ASKED for, not what the popup settled on
  const realOpen = window.open; window.__opened = [];
  window.open = (u, n, f) => { window.__opened.push({ u, n, f }); return realOpen.call(window, u, n, f); };
  out.hiddenOnRung = !btn.classList.contains('show');
  sessionList.push(fake);
  focusSession(sessionList.find(s => s.cid === CID));
  out.inTty = currentView() === 'tty' && currentCid === CID;
  out.shownInTty = btn.classList.contains('show') && getComputedStyle(btn).display !== 'none';
  out.headerUp = getComputedStyle(document.querySelector('header')).display !== 'none';
  out.notBreakout = !document.documentElement.classList.contains('breakout');
  const rc = btn.getBoundingClientRect(); out.rc = [rc.left + rc.width / 2, rc.top + rc.height / 2];
  const hit = document.elementFromPoint(...out.rc); out.reachable = hit === btn;
  return out;
}, { CID, fake: fake(CID) });
console.log('MAIN:', JSON.stringify(r1));

// a REAL click → a popup
const [popup] = await Promise.all([
  context.waitForEvent('page', { timeout: 5000 }).catch(() => null),
  page.mouse.click(r1.rc[0], r1.rc[1]),
]);
const r2 = { opened: !!popup };
if (popup) {
  await popup.waitForLoadState('domcontentloaded');
  await popup.waitForFunction(() => typeof focusSession === 'function' && window.__stubbed).catch(() => {});
  await popup.waitForTimeout(800);
  const asked = await page.evaluate(() => window.__opened);
  const u = new URL(asked[0].u, 'http://127.0.0.1:8787/');
  r2.asked = asked[0].u;
  r2.flag = u.searchParams.get('breakout') === '1' && new URL(popup.url()).searchParams.get('breakout') === '1';
  r2.hashOk = u.hash === `#/p/self/s/${CID}/tty`;
  r2.named = asked[0].n === 'breakout:' + CID && /popup=1/.test(asked[0].f);
  r2.noToken = !u.searchParams.has('t');
  Object.assign(r2, await popup.evaluate(({ CID, fake }) => {
    const out = {};
    out.cls = document.documentElement.classList.contains('breakout');
    const gone = (sel) => getComputedStyle(document.querySelector(sel)).display === 'none';
    out.headerGone = gone('header'); out.barGone = gone('#sessionbar'); out.ironGone = gone('#ironrow'); out.needsGone = gone('#needsbar');
    // land on the (fake) session in here too, the way the boot resolve would with a real cid
    sessionList.push(fake);
    focusSession(sessionList.find(s => s.cid === CID));
    out.inTty = currentView() === 'tty' && currentCid === CID;
    out.ejectGone = gone('#ejectBtn');
    out.closeUp = getComputedStyle(document.getElementById('closeBtn')).display !== 'none';
    const term = document.getElementById('term').getBoundingClientRect();
    out.paneAtTop = term.top < 2 && term.height > 200;                  // nothing above the pane
    out.footerUp = getComputedStyle(document.querySelector('footer')).display !== 'none';
    out.title = document.title;
    out.titled = document.title === '⏏ eject-me';
    out.nothingSent = !(window.__sent || []).some(f => f && (f.type === 'send' || f.type === 'input' || f.type === 'close'));
    return out;
  }, { CID, fake: fake(CID) }));
  // ejecting the same session again must REUSE the window (named by cid), not open a second one
  const second = await Promise.all([
    context.waitForEvent('page', { timeout: 1500 }).then(() => true).catch(() => false),
    page.mouse.click(r1.rc[0], r1.rc[1]),
  ]);
  r2.noDuplicate = second[0] === false;
}
console.log('POPUP:', JSON.stringify(r2));

const r3 = await page.evaluate((CID) => {
  const i = sessionList.findIndex(s => s.cid === CID); if (i !== -1) sessionList.splice(i, 1);
  currentCid = null; setView('sessions'); renderSessionBar();
  return { nothingSent: !(window.__sent || []).some(f => f && (f.type === 'send' || f.type === 'input' || f.type === 'close')) };
}, CID);

const ok = r1.hiddenOnRung && r1.inTty && r1.shownInTty && r1.headerUp && r1.notBreakout && r1.reachable
  && r2.opened && r2.flag && r2.hashOk && r2.named && r2.noToken && r2.cls && r2.headerGone && r2.barGone && r2.ironGone && r2.needsGone
  && r2.inTty && r2.ejectGone && r2.closeUp && r2.paneAtTop && r2.footerUp && r2.titled && r2.nothingSent && r2.noDuplicate
  && r3.nothingSent;
console.log(ok ? 'PASS — ⏏ shows in a session, a real click pops a breakout window with only the pane + composer, same session, no duplicate'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
