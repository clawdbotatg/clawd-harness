#!/usr/bin/env node
// e2eresyncprobe — the page must never keep an E2E channel 'open' that the
// worker can't read (2026-09-11: every tty black, fleet-wide, through ten
// reloads). A record the worker can't open is dropped with NO reply, so the
// page has to notice desync on its own. Three guards, driven against a fake
// relay (no worker, no relay, no real session):
//
//   1. newest resume wins — a later `e2e.resumed` (a previous attempt's late
//      answer, or one that lands mid-derivation) re-keys the open channel to
//      the worker's newest nonce instead of leaving us on a stale one;
//   2. "no session" / "expired" on an open channel rebuilds it (silent resume),
//      re-lists, and never raises a passkey when there's no material;
//   3. frames out, nothing back → the deaf watchdog rebuilds, at most once a
//      minute.
//
//   cd tools && node e2eresyncprobe.mjs
//
// Exit code is non-zero if a check fails — so it works in a verify flow.

import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);

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

const exec = findChromium();
if (!exec) { console.error('No cached playwright chromium found. Run: cd tools && npx playwright install chromium'); process.exit(2); }

const html = readFileSync(join(ROOT, 'index.html'), 'utf8')
  .replace('<head>', '<head><script>window.__FLEET__=true;</script>');

const b64u = (buf) => Buffer.from(buf).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const MASTER = b64u(randomBytes(32));
const M = 'clawd-x';

const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 500, height: 900 }, hasTouch: true });

await page.addInitScript(({ master, m }) => {
  window.__sent = [];
  const sockets = [];
  class FakeWS {
    constructor(url) {
      this.url = url; this.readyState = 0; this.binaryType = 'arraybuffer';
      sockets.push(this);
      setTimeout(() => { this.readyState = 1; this.onopen && this.onopen({}); }, 0);
    }
    send(data) { window.__sent.push(data); }
    close() { this.readyState = 3; this.onclose && this.onclose({}); }
  }
  FakeWS.prototype.addEventListener = function () {};
  window.WebSocket = FakeWS;
  window.__relayRx = (obj) => {
    const s = sockets[sockets.length - 1];
    if (s && s.onmessage) s.onmessage({ data: JSON.stringify(obj) });
  };
  try { localStorage.clear(); } catch {}
  try { localStorage.setItem('cc_e2e_rs_' + m, JSON.stringify({ id: 'probe-rs', master, exp: Date.now() + 3600e3 })); } catch {}
}, { master: MASTER, m: M });

const errors = [];
page.on('pageerror', e => errors.push(String(e)));
await page.route('https://fleet.probe/', route =>
  route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html }));

let failed = false;
const check = (name, ok, detail) => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${ok || !detail ? '' : ' — ' + detail}`);
  if (!ok) failed = true;
};

const ROSTER = { type: 'machines', machines: [
  { id: M, host: 'x', kind: 'machine', online: true, lastSeen: 0, stats: { projects: 1, sessions: 1, active: 0 } },
] };
const rx = (obj) => page.evaluate(o => window.__relayRx(o), obj);
const sent = async () => (await page.evaluate(() => window.__sent))
  .map(s => { try { return JSON.parse(s); } catch { return null; } }).filter(Boolean);
const ctl = async (t) => (await sent()).filter(f => f.type === 'toMachine' && f.machine === M && f.msg && f.msg.t === t);
const clearSent = () => page.evaluate(() => { window.__sent = []; });
// a worker's resumed reply for nonce rn, confirmed with the page's own crypto
const resumed = async (rnB64) => {
  const cf = await page.evaluate(async ({ master, rn }) => b64uEnc(await E2E.resumeConfirm(b64uDec(master), b64uDec(rn))), { master: MASTER, rn: rnB64 });
  return { type: 'machineMsg', machine: M, msg: { t: 'e2e.resumed', rn: rnB64, cf } };
};
const chan = () => page.evaluate(m => { const c = e2eChans[m]; return c ? { status: c.status, inst: c.inst, rn: c.rn || null } : null; }, M);
const ageChan = () => page.evaluate(m => { const c = e2eChans[m]; if (c) c.openedAt = Date.now() - 5000; }, M);

await page.goto('https://fleet.probe/', { waitUntil: 'domcontentloaded', timeout: 15000 });
await page.waitForTimeout(500);
await rx({ type: 'prefs', inactive: [] });
await rx(ROSTER);
await page.waitForTimeout(700);

// -- 0. bootstrap resumes off the stored material ------------------------------
check('bootstrap sends a silent resume', (await ctl('e2e.resume')).length >= 1);
const rnA = b64u(randomBytes(32));
await rx(await resumed(rnA));
await page.waitForTimeout(400);
let c = await chan();
check('channel opens on the worker\'s reply', c && c.status === 'open' && c.rn === rnA, JSON.stringify(c));
const inst0 = c && c.inst;

// -- 1. newest resume wins ----------------------------------------------------
const rnB = b64u(randomBytes(32));
await rx(await resumed(rnB));
await page.waitForTimeout(400);
c = await chan();
check('a later resumed reply re-keys the OPEN channel to the newer nonce', c && c.status === 'open' && c.rn === rnB && c.inst === inst0, JSON.stringify(c));

// a forged one (bad confirm) must not
const rnX = b64u(randomBytes(32));
await rx({ type: 'machineMsg', machine: M, msg: { t: 'e2e.resumed', rn: rnX, cf: b64u(randomBytes(32)) } });
await page.waitForTimeout(400);
c = await chan();
check('a resumed reply that fails confirm is ignored', c && c.rn === rnB, JSON.stringify(c));

// two replies landing back-to-back mid-derivation: the second must win
await page.evaluate(m => { delete e2eChans[m]; e2eEnsure(m, { reason: 'probe' }).catch(() => {}); }, M);
await page.waitForTimeout(150);
const rnC = b64u(randomBytes(32)), rnD = b64u(randomBytes(32));
const rC = await resumed(rnC), rD = await resumed(rnD);
await page.evaluate(([a, b]) => { window.__relayRx(a); window.__relayRx(b); }, [rC, rD]);
await page.waitForTimeout(500);
c = await chan();
check('two replies mid-handshake → the channel ends on the second (worker\'s newest)', c && c.status === 'open' && c.rn === rnD, JSON.stringify(c));

// -- 2. "no session" on an open channel rebuilds it silently ---------------------
await ageChan();
const instBefore = (await chan()).inst;
await clearSent();
await rx({ type: 'machineMsg', machine: M, msg: { t: 'e2e.err', error: 'no session' } });
await page.waitForTimeout(300);
c = await chan();
check('"no session" → channel replaced and a fresh resume goes out', c && c.inst > instBefore && (await ctl('e2e.resume')).length === 1, JSON.stringify(c));
check('…with no passkey prompt', await page.$eval('#passkey', e => e.hidden));
const rnE = b64u(randomBytes(32));
await rx(await resumed(rnE));
await page.waitForTimeout(400);
c = await chan();
const recs = await ctl('e2e.rec');
check('the rebuilt channel opens and re-lists', c && c.status === 'open' && c.rn === rnE && recs.length >= 1, JSON.stringify({ c, recs: recs.length }));

// a "no session" answering a record that predates a channel just opened is ignored
await clearSent();
await rx({ type: 'machineMsg', machine: M, msg: { t: 'e2e.err', error: 'no session' } });
await page.waitForTimeout(200);
check('a "no session" in the first moment after opening is a straggler, not a rebuild', (await chan()).inst === c.inst && (await ctl('e2e.resume')).length === 0);

// -- 3. deaf watchdog ---------------------------------------------------------
// shorten the window; the re-list above already armed a timer at the production
// length, so drop it — the next send re-arms at the probe length
await page.evaluate(m => { E2E_DEAF_MS = 300; const c = e2eChans[m]; clearTimeout(c.deafTimer); c.deafTimer = null; c.sentSinceRecv = 0; }, M);
await ageChan();
await clearSent();
const instDeaf = (await chan()).inst;
await page.evaluate(m => { for (let i = 0; i < 3; i++) e2eSendFrame(m, { type: 'list' }); }, M);
await page.waitForTimeout(900);
c = await chan();
check('3 frames out, nothing back → rebuild (fresh resume)', c && c.inst > instDeaf && (await ctl('e2e.resume')).length === 1, JSON.stringify(c));
const rnF = b64u(randomBytes(32));
await rx(await resumed(rnF));
await page.waitForTimeout(300);
await ageChan();
await clearSent();
const instRate = (await chan()).inst;
await page.evaluate(m => { for (let i = 0; i < 3; i++) e2eSendFrame(m, { type: 'list' }); }, M);
await page.waitForTimeout(900);
check('…but at most once a minute (no churn on a quiet link)', (await chan()).inst === instRate && (await ctl('e2e.resume')).length === 0);

// -- 4. "expired" with dead material: drop the channel, no uninvited passkey ------
await ageChan();
await clearSent();
await rx({ type: 'machineMsg', machine: M, msg: { t: 'e2e.err', error: 'expired' } });
await page.waitForTimeout(500);
const gone = await page.evaluate(m => !e2eChans[m] || e2eChans[m].status !== 'open', M);
const mat = await page.evaluate(m => localStorage.getItem('cc_e2e_rs_' + m), M);
check('"expired" → material cleared, channel dropped', gone && !mat, JSON.stringify({ gone, mat }));
check('…and no background handshake / passkey prompt', (await ctl('e2e.hello')).length === 0 && await page.$eval('#passkey', e => e.hidden));

check('no page errors', errors.length === 0, errors.join(' | '));

await browser.close();
process.exit(failed ? 1 : 0);
