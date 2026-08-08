// fleetprobe — drive the page in FLEET mode against a fake relay, and assert the
// two things that decide how many passkeys you pay at dawn.
//
//   cd tools && node fleetprobe.mjs
//
// 1. ACTIVE MACHINES. Talking to a machine costs one passkey per 24h, so a fleet
//    of N boxes costs N ceremonies even when you only wanted one of them. A
//    machine switched off in the machines tab must receive ZERO frames — no
//    channel, no handshake, no prompt — and its projects must leave the rungs.
//    The set is stored on the relay (`prefs`), so this also checks the page
//    honours a set pushed from another device.
//
// 2. MODAL OWNERSHIP. The relay re-broadcasts the roster every 20s as a liveness
//    heartbeat, and the edge auth and each machine's E2E handshake share ONE
//    #passkey modal. The `machines` handler used to hide it blindly, which yanked
//    a half-finished per-machine unlock off the screen: the button that fires the
//    assertion vanished, the handshake never completed, and the page sat blank
//    until a manual reload ("I authed once, got a blank screen, had to reload").
//
// Needs no relay and no worker: window.WebSocket is stubbed before the page
// loads, so nothing leaves this machine and no real session is touched. The page
// itself is read off disk with the relay's own __FLEET__ injection.
//
// Exit code is non-zero if a check fails — so it works in a verify flow.

import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

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

// The page exactly as the relay serves it (fleet mode).
const html = readFileSync(join(ROOT, 'index.html'), 'utf8')
  .replace('<head>', '<head><script>window.__FLEET__=true;</script>');

const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 500, height: 900 } });

// -- fake relay --------------------------------------------------------------
// A WebSocket stand-in the probe drives from Node: __relayRx(frame) pushes a
// frame at the page, __relaySent() returns everything the page has sent.
await page.addInitScript(() => {
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
  // Storage starts clean so a previous run's choices can't colour this one.
  try { localStorage.clear(); } catch {}
  // Give every machine unexpired E2E resume material — i.e. the ordinary
  // "seen it earlier today" state. fleetBootstrap deliberately serializes the
  // machines that need a FRESH passkey (one shared modal), each with a 20s
  // timeout, so without this a 3-machine probe would take a minute to prove
  // something that has nothing to do with ordering. Resumable machines all
  // reach out at once, which is what we're counting. Nobody answers, so no
  // channel ever opens — we only care WHO was spoken to.
  for (const m of ['clawd-atg', 'clawd-head', 'clawd-heart']) {
    try { localStorage.setItem('cc_e2e_rs_' + m,
      JSON.stringify({ id: 'probe-' + m, master: 'AAAA', exp: Date.now() + 3600e3 })); } catch {}
  }
});

const errors = [];
page.on('pageerror', e => errors.push(String(e)));

await page.route('https://fleet.probe/', route =>
  route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html }));

let failed = false;
const check = (name, ok, detail) => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${ok || !detail ? '' : ' — ' + detail}`);
  if (!ok) failed = true;
};

const ROSTER = {
  type: 'machines',
  machines: [
    { id: 'clawd-atg',   host: 'atg',   kind: 'machine', online: true, lastSeen: 0, stats: { projects: 2, sessions: 3, active: 1 } },
    { id: 'clawd-head',  host: 'head',  kind: 'machine', online: true, lastSeen: 0, stats: { projects: 1, sessions: 1, active: 0 } },
    { id: 'clawd-heart', host: 'heart', kind: 'machine', online: true, lastSeen: 0, stats: { projects: 1, sessions: 0, active: 0 } },
    { id: 'clawd-nerve-cord', host: 'hub', kind: 'relay', online: true, lastSeen: 0, stats: null },
  ],
};

const rx = (obj) => page.evaluate(o => window.__relayRx(o), obj);
const sent = async () => (await page.evaluate(() => window.__sent))
  .map(s => { try { return JSON.parse(s); } catch { return null; } }).filter(Boolean);
// Which machines has the page tried to open a channel to? An E2E hello/resume is
// the first frame of a handshake — and the handshake is what costs a passkey.
const reached = async () => [...new Set((await sent())
  .filter(f => f.type === 'toMachine')
  .map(f => f.machine))].sort();
const clearSent = () => page.evaluate(() => { window.__sent = []; });

await page.goto('https://fleet.probe/', { waitUntil: 'domcontentloaded', timeout: 15000 });
await page.waitForTimeout(600);

// -- 1. everything on: the page reaches every real machine (today's behaviour) --
await rx({ type: 'prefs', inactive: [] });
await rx(ROSTER);
await page.waitForTimeout(900);
let got = await reached();
check('all machines on → a channel is opened to each (one passkey each)',
      got.length === 3 && got.join() === 'clawd-atg,clawd-head,clawd-heart', JSON.stringify(got));
check('the relay hub is never handshaked', !got.includes('clawd-nerve-cord'), JSON.stringify(got));

// -- 2. switch two off from the machines tab ---------------------------------
await page.evaluate(() => window.openMachines && window.openMachines());
await page.evaluate(() => {
  // the checkbox the user actually taps, found by its label text
  for (const lab of document.querySelectorAll('#machinelist .cdefault')) {
    if (!/talk to this machine/.test(lab.textContent)) continue;
    const id = lab.closest('.scard').querySelector('.stitle').textContent;
    if (id === 'clawd-head' || id === 'clawd-heart') {
      const cb = lab.querySelector('input');
      cb.checked = false; cb.onchange();
    }
  }
});
await page.waitForTimeout(300);

const wrote = (await sent()).filter(f => f.type === 'prefs').pop();
check('unchecking writes the set to the relay (so other devices follow)',
      wrote && (wrote.inactive || []).sort().join() === 'clawd-head,clawd-heart',
      JSON.stringify(wrote));

const cards = await page.$$eval('#machinelist .scard', els => els.map(e => ({
  id: e.querySelector('.stitle')?.textContent || '',
  tag: e.querySelector('.scount')?.textContent || '',
})));
check('the off machines are still listed (so you can switch them back on)',
      cards.filter(c => c.tag === 'off').map(c => c.id).sort().join() === 'clawd-head,clawd-heart',
      JSON.stringify(cards));

// -- 3. THE POINT: tomorrow morning, only the machine you kept costs a passkey -
// A fresh load with the stored set — the relay always sends `prefs` before the
// first roster, which is what stops the page unlocking boxes you switched off.
await page.reload({ waitUntil: 'domcontentloaded', timeout: 15000 });
await page.waitForTimeout(600);
await rx({ type: 'prefs', inactive: ['clawd-head', 'clawd-heart'] });
await rx(ROSTER);
await page.waitForTimeout(1200);
got = await reached();
check('next load: only the machine you kept is contacted (one passkey, not three)',
      got.length === 1 && got[0] === 'clawd-atg', JSON.stringify(got));

// -- 4. a set pushed from another device is honoured here ---------------------
await clearSent();
await rx({ type: 'prefs', inactive: ['clawd-atg', 'clawd-head', 'clawd-heart'] });
await rx(ROSTER);
await page.waitForTimeout(900);
got = await reached();
check('a set pushed from another device switches machines off here too',
      got.length === 0, JSON.stringify(got));

await clearSent();
await rx({ type: 'prefs', inactive: ['clawd-head', 'clawd-heart'] });
await page.waitForTimeout(900);
got = await reached();
check('…and switching one back on there opens its channel here',
      got.join() === 'clawd-atg', JSON.stringify(got));

// -- 5. the heartbeat must not close a machine's passkey prompt ---------------
// The real prompt is raised mid-handshake (after a worker ServerHello we can't
// fake without keys), so drive the same modal through the same ownership path.
await page.evaluate(() => window.showPasskey('unlock clawd-atg — whisper the safe word', 'clawd-atg'));
const upBefore = await page.$eval('#passkey', e => !e.hidden);
check('a machine can raise the passkey modal', upBefore);
await rx(ROSTER);                       // the 20s liveness heartbeat lands mid-prompt
await rx({ type: 'authOk' });           // …and so does an edge re-auth
await page.waitForTimeout(300);
check('the roster heartbeat does NOT close it (the blank-screen bug)',
      await page.$eval('#passkey', e => !e.hidden));
await page.evaluate(() => window.hidePasskey('clawd-atg'));
check('its owner can close it', await page.$eval('#passkey', e => e.hidden));

// and the edge prompt is still closable by the roster, as it always was
await page.evaluate(() => window.showPasskey('prove it\'s you', 'edge'));
await rx(ROSTER);
await page.waitForTimeout(200);
check('an EDGE prompt still closes on the roster that follows authOk',
      await page.$eval('#passkey', e => e.hidden));

check('no uncaught page errors', errors.length === 0, errors.join(' | '));

const shot = join(HERE, 'fleetprobe.png');
await page.evaluate(() => window.openMachines && window.openMachines());
await page.screenshot({ path: shot, fullPage: false });
console.log('\nscreenshot ->', shot);
await browser.close();
process.exit(failed ? 1 : 0);
