// spawnprobe — a `new` that never comes back must not be a black void, and the
// prompt typed into it must never be lost.
//
//   cd tools && node spawnprobe.mjs
//
// 2026-08-20: a viewer came back after a long idle; the fleet-wide E2E resume
// round stalled ~90s; the user hit ＋, typed a prompt into that gap, stared at a
// cursor on black, reloaded — and both the session and the prompt were gone. The
// `new` (and the queued `send`) had been parked on a channel that never opened,
// and flushPendingSend's 8s fallback had spent the reload-recovery copy into the
// void. This drives the page in DIRECT mode against a stubbed WebSocket that
// simply never answers a `new`, and asserts:
//
//   1. asking paints a "starting…" island (not a void) and sends exactly a `new`
//   2. the queued text is NOT sent while no session exists
//   3. when no focus lands in NEW_FOCUS_WAIT_MS the page stands down: back on the
//      sessions rung, the text back in the box, no stray `send`
//   4. when the focus DOES land, the island goes away and the text is delivered
//   5. a reload mid-void lands on the sessions rung with the text restored
//
// No harness, no relay, no session: window.WebSocket is stubbed before the page
// loads. Exit code is non-zero if a check fails.

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

const html = readFileSync(join(ROOT, 'index.html'), 'utf8');
const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });

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
  window.__rx = (obj) => { const s = sockets[sockets.length - 1]; if (s && s.onmessage) s.onmessage({ data: JSON.stringify(obj) }); };
});

const errors = [];
page.on('pageerror', e => errors.push(String(e)));
await page.route('https://spawn.probe/**', route =>
  route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html }));

let failed = false;
const check = (name, ok, detail) => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${ok || !detail ? '' : ' — ' + detail}`);
  if (!ok) failed = true;
};
const rx = (obj) => page.evaluate(o => window.__rx(o), obj);
const sent = async () => (await page.evaluate(() => window.__sent))
  .map(s => { try { return JSON.parse(s); } catch { return null; } }).filter(Boolean);
const clearSent = () => page.evaluate(() => { window.__sent = []; });
const state = () => page.evaluate(() => ({
  view: currentView(), pid: currentPid, cid: currentCid, box: box.value,
  veil: !deadveil.hidden, veilText: deadveil.textContent, meta: meta.textContent,
  pendingNewFocus, pendingSendText,
}));
const PROJECTS = { type: 'projects', boot: 'probe-boot', projects: [
  { pid: 'p1', name: 'probe', path: '/tmp/probe', repoUrl: '', status: 'ready', kind: 'gh' } ] };
const WAIT = 2500;   // the probe's NEW_FOCUS_WAIT_MS (30s in production)

async function boot(hash) {
  await page.goto('https://spawn.probe/?t=probe' + hash, { waitUntil: 'domcontentloaded', timeout: 15000 });
  await page.waitForTimeout(500);
  await rx(PROJECTS);
  await rx({ type: 'sessions', sessions: [] });
  await page.waitForTimeout(400);
  await page.evaluate(w => { NEW_FOCUS_WAIT_MS = w; }, WAIT);
}

await boot('#/p/p1');
let st = await state();
check('boot lands on the sessions rung of the project', st.view === 'sessions' && st.pid === 'p1', JSON.stringify(st));

// ── 1–3: a `new` nobody answers ─────────────────────────────────────────────
const TEXT = 'hello from the void — keep me';
await clearSent();
await page.evaluate(t => { box.value = t; sendMessage(); }, TEXT);
await page.waitForTimeout(300);
st = await state();
let fr = await sent();
check('asking opens the tty view with a "starting…" island, not a void',
      st.view === 'tty' && st.veil && /starting/.test(st.veilText), JSON.stringify(st));
check('exactly one `new` went out for the project', fr.filter(f => f.type === 'new' && f.pid === 'p1').length === 1, JSON.stringify(fr));
check('the box is cleared (the text is queued, not lost)', st.box === '' && st.pendingSendText === TEXT);

await page.waitForTimeout(WAIT + 800);
st = await state();
fr = await sent();
check('no focus in NEW_FOCUS_WAIT_MS → back on the sessions rung', st.view === 'sessions' && !st.veil, JSON.stringify(st));
check('…with the text back in the box', st.box === TEXT, JSON.stringify(st.box));
check('…and the queued send was never fired into the void', !fr.some(f => f.type === 'send'), JSON.stringify(fr));
check('…and the flag is cleared so the next real focus is not hijacked', st.pendingNewFocus === false && st.pendingSendText === null);
check('the meta line says what happened', /back in the box/.test(st.meta), st.meta);

// ── 4: the focus DOES land ──────────────────────────────────────────────────
await clearSent();
await page.evaluate(() => { sendMessage(); });   // box still holds TEXT
await page.waitForTimeout(200);
await rx({ type: 'focus', cid: 'c1' });
await rx({ type: 'sessions', sessions: [{ cid: 'c1', pid: 'p1', title: 'probe session', alive: true, status: 'idle', account: 'default' }] });
await page.waitForTimeout(300);
st = await state();
check('a focus reply opens the session and drops the island', st.view === 'tty' && st.cid === 'c1' && !st.veil, JSON.stringify(st));
await rx({ type: 'hook', cid: 'c1', event: 'SessionStart' });
await page.waitForTimeout(900);
fr = await sent();
check('the queued text is delivered once the session exists', fr.some(f => f.type === 'send' && f.text === TEXT), JSON.stringify(fr));
await page.waitForTimeout(WAIT + 500);
st = await state();
check('the watch does not fire on a session that did spawn', st.view === 'tty' && st.cid === 'c1', JSON.stringify(st));

// ── 5: reload mid-void ──────────────────────────────────────────────────────
const TEXT2 = 'typed, reloaded, still here';
await page.evaluate(() => { pendingSendText = null; setView('sessions'); });
await page.waitForTimeout(200);
await page.evaluate(t => { box.value = t; sendMessage(); }, TEXT2);
await page.waitForTimeout(600);
st = await state();
check('second ask is parked on the island again', st.view === 'tty' && st.veil, JSON.stringify(st));
await page.reload({ waitUntil: 'domcontentloaded', timeout: 15000 });
await page.waitForTimeout(500);
await rx(PROJECTS);
await rx({ type: 'sessions', sessions: [] });
await page.waitForTimeout(600);
st = await state();
check('a reload mid-void lands on the sessions rung', st.view === 'sessions' && st.pid === 'p1', JSON.stringify(st));
check('…with the text restored to the box', st.box === TEXT2, JSON.stringify(st.box));

check('no uncaught page errors', errors.length === 0, errors.join(' | '));
await browser.close();
process.exit(failed ? 1 : 0);
