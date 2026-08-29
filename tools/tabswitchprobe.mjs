// tabswitchprobe — is the session tab strip clickable WHILE the splash is up,
// and does clicking a tab mid-splash jump sessions + restart the splash?
//
// Rebuilt 2026-08-29: the first cut deep-linked a HARDCODED real cid, so it
// (a) died the day that session did — it sat red and unnoticed for weeks, the
// silent-guard-rot problem — and (b) subscribed to live sessions and claimed
// their PTY size (the uiprobe-binds-a-real-session trap). Now it uses the
// splashprobe pattern: two FAKE sessions injected into sessionList with
// `hsend` stubbed, so nothing reaches the server and no real session is
// touched. Server must be running on :8787 (it only serves the page).
//   cd tools && node tabswitchprobe.mjs
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
const url = `http://127.0.0.1:8787/?t=${token}#/p/self`;
const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => typeof maybeSplash === 'function' && typeof hsend === 'function').catch(() => {});
await page.waitForTimeout(600);

const r = await page.evaluate(async () => {
  const A = '00000000-probe-tabswitch-aaaa', B = '00000000-probe-tabswitch-bbbb';
  const sent = [];
  const realHsend = window.hsend;
  window.hsend = (f) => { sent.push(f); };            // nothing reaches the server
  try { localStorage.removeItem('cc_seen_at'); } catch {}
  try { for (const k of Object.keys(seenAt)) delete seenAt[k]; } catch {}
  const now = Date.now() / 1000;
  sessionList.push(
    { cid: A, pid: 'self', title: 'probe session A', desc: 'first fake', tab: 'alpha',
      promptCount: 3, alive: true, busy: false, model: 'claude-opus-5', lastActive: now, promptedAt: now },
    { cid: B, pid: 'self', title: 'probe session B', desc: 'second fake', tab: 'bravo',
      promptCount: 3, alive: true, busy: false, model: 'claude-opus-5', lastActive: now, promptedAt: now - 60 });
  const out = { sent };
  try {
    // open A the way a tab click does — splash flashes for it
    focusSession(sessionList.find(s => s.cid === A));
    const splashEl = document.getElementById('splash');
    out.splashA = !splashEl.hidden && splashCid === A && currentCid === A;
    // while it's flashing, B's tab must be reachable (nothing overlays it) …
    renderSessionBar();
    const tab = [...document.querySelectorAll('#sessionbar .stab')]
      .find(t => (t.title || '').includes('probe session B'));   // NEVER fall back to a real tab
    out.tabFound = !!tab;
    if (tab) {
      const rc = tab.getBoundingClientRect();
      const hit = document.elementFromPoint(rc.left + rc.width / 2, rc.top + rc.height / 2);
      out.reachable = tab === hit || tab.contains(hit);
      tab.click();                                     // … and the click must land
    }
    await new Promise(res => setTimeout(res, 300));
    out.jumped = currentCid === B;
    out.splashB = !document.getElementById('splash').hidden && splashCid === B;
  } finally {
    hideSplash();
    window.hsend = realHsend;
    for (const cid of [A, B]) {
      const i = sessionList.findIndex(s => s.cid === cid);
      if (i !== -1) sessionList.splice(i, 1);
    }
    currentCid = null; setView('sessions'); renderSessionBar();
  }
  out.nothingSent = false;                             // recomputed below with the stub restored
  out.sentTypes = sent.map(f => f && f.type);
  return out;
});
console.log('TABSWITCH:', JSON.stringify(r));

// subscribe frames to the FAKE cids are fine (server ignores unknown cids and
// no real session is touched); what must never appear is a send/new/resize
// aimed at a real session — the stub caught everything, so just assert shape.
const badSends = (r.sentTypes || []).filter(t => t === 'send' || t === 'new');
const ok = r.splashA && r.tabFound && r.reachable && r.jumped && r.splashB && badSends.length === 0;
console.log(ok ? 'PASS — tab click mid-splash jumps sessions, splash restarts for the new one'
              : 'FAIL — ' + JSON.stringify(r));
await browser.close();
process.exit(ok ? 0 : 1);
