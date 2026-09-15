// checkprobe — 🔍 double-check: the OTHER engine reviews a session's work
// (2026-09-13). The harness ARMS the source (check frame), it writes a local
// REVIEW-<stamp>.md, the Stop hook spawns a reviewer tab (checkOf = source)
// briefed with the file + the diff; the reviewer's Stop hands the verdict back
// to the source (server-side). Guards the client half, on emulated touch with
// REAL taps (three production bugs were invisible to element.click()):
//   1. a tap on 🔍 sends {type:'check', cid} and STAYS on the source (no blank
//      terminal, no pending focus) — the meta line says the brief is coming;
//   2. checkArmed in a sessions frame → the row above the composer names the
//      reviewer engine + 🔍 on the source's tab;
//   3. the reviewer arriving (checkOf = source) while we still watch the
//      source → we land on it; its seeded 🔍 title is not doubled;
//   4. a reviewer arriving after the viewer moved on → no jump (tab in strip);
//   5. a tap on cancel sends checkCancel, drops the row, and a reviewer
//      arriving afterwards never jumps;
//   6. a refusal (error carrying check: cid) → meta says why, view untouched;
//   7. a renamed reviewer (namer dropped the 🔍) still wears 🔍 on its tab.
// Safe: the page is served from memory at a fake origin with WebSocket
// stubbed — nothing reaches the real harness, no session is touched.
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = dirname(HERE);
function findChromium() {
  const c = join(process.env.HOME, 'Library/Caches/ms-playwright');
  for (const d of readdirSync(c).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse())
    for (const a of ['mac-arm64', 'mac-x64']) { const b = join(c, d, `chrome-headless-shell-${a}`, 'chrome-headless-shell'); if (existsSync(b)) return b; }
}
const raw = readFileSync(join(ROOT, 'index.html'), 'utf8');
const browser = await chromium.launch({ executablePath: findChromium() });
const ctx = await browser.newContext({ viewport: { width: 900, height: 900 }, hasTouch: true });
const page = await ctx.newPage();
const errors = [];
let failed = false; const check = (n, ok, d) => { console.log(`  ${ok ? '✓' : '✗'} ${n}${ok || !d ? '' : ' — ' + d}`); if (!ok) failed = true; };
await page.addInitScript(() => {
  window.__sent = []; const sockets = [];
  class FakeWS { constructor(u) { this.url = u; this.readyState = 0; this.binaryType = 'arraybuffer'; sockets.push(this);
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen({}); }, 0); }
    send(d) { window.__sent.push(d); } close() { this.readyState = 3; this.onclose && this.onclose({}); } }
  FakeWS.prototype.addEventListener = function () {}; window.WebSocket = FakeWS;
  window.__rx = (o) => { const s = sockets[sockets.length - 1]; if (s && s.onmessage) s.onmessage({ data: JSON.stringify(o) }); };
  try { localStorage.clear(); } catch {}
});
page.on('pageerror', e => errors.push(String(e)));
const url = 'https://direct.probe/?t=x';
await page.route(url, r => r.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: raw }));
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });
await page.waitForTimeout(500);

const A = { cid: 'ca', pid: 'p1', title: 'alpha build', tab: 'alpha', alive: true, busy: false, pinned: 0, promptedAt: 1, lastActive: 2, engine: 'claude' };
const B = { cid: 'cb', pid: 'p1', title: 'beta work', tab: 'beta', alive: true, busy: false, pinned: 0, promptedAt: 1, lastActive: 1, engine: 'claude' };
const R = { cid: 'cr', pid: 'p1', title: '🔍 alpha build', tab: '', alive: true, busy: true, pinned: 0, promptedAt: 3, lastActive: 3, engine: 'codex', checkOf: 'ca' };
const sent = () => page.evaluate(() => window.__sent.map(x => { try { return JSON.parse(x); } catch { return null; } }).filter(Boolean));
const rx = (sessions) => page.evaluate((sessions) => { window.__rx({ type: 'sessions', sessions }); }, sessions);
const tabs = () => page.evaluate(() => [...document.querySelectorAll('#sessionbar .stab')].map(t => t.querySelector('.lbl').textContent));
const state = () => page.evaluate(() => ({ view: currentView(), cid: currentCid, pending: pendingNewFocus === true,
  row: !document.getElementById('checkrow').hidden, text: document.getElementById('checktext').textContent,
  meta: document.getElementById('meta').textContent, src: checkSrcCid }));
await page.evaluate(({ A, B }) => {
  window.__rx({ type: 'projects', projects: [{ pid: 'p1', name: 'alpha', repoUrl: 'https://github.com/x/alpha', kind: 'gh', status: 'ready', sessionCount: 2, busyCount: 0, waitingCount: 0, created: 1, pinned: false, lastTouched: 10, emoji: '' }], boot: 'b1' });
  window.__rx({ type: 'sessions', sessions: [A, B], current: null });
  window.__rx({ type: 'irons', irons: [] });
  window.__rx({ type: 'closed', closed: [] });
}, { A, B });
await page.waitForTimeout(300);
await page.evaluate(() => { focusSession(allSessions().find(s => s.cid === 'ca')); window.__sent.length = 0; });
await page.waitForTimeout(400);
let st = await state();
check('the alpha tab opens its tty', st.view === 'tty' && st.cid === 'ca', JSON.stringify(st));

// 1. a REAL tap on 🔍
const btn = page.locator('#checkBtn');
await btn.scrollIntoViewIfNeeded();
await btn.tap();
await page.waitForTimeout(300);
let f = await sent();
const chk = f.find(x => x.type === 'check');
check('🔍 sends a check frame for the open session', !!chk && chk.cid === 'ca', JSON.stringify(f.map(x => x.type)));
check('…and NOT a send, new, or fork', !f.some(x => ['send', 'new', 'fork'].includes(x.type)));
st = await state();
check('…we stay on the source, tty intact, no pending focus', st.cid === 'ca' && st.view === 'tty' && !st.pending && st.src === 'ca', JSON.stringify(st));
check('…the meta line says a brief is coming', /brief/.test(st.meta), st.meta);

// 2. armed → the row + the badge
await rx([{ ...A, checkArmed: true }, B]);
await page.waitForTimeout(200);
st = await state(); let t = await tabs();
check('checkArmed → the 🔍 row names the reviewer engine', st.row && /codex reviewer/.test(st.text), JSON.stringify(st));
check('…and the source tab wears 🔍', t.some(x => x.startsWith('🔍 alpha')), JSON.stringify(t));

// 3. the reviewer arrives while we watch the source → land on it
await rx([{ ...A, checkArmed: false }, B, R]);
await page.waitForTimeout(400);
st = await state(); t = await tabs();
check('reviewer (checkOf = source) arrived → we land on it', st.cid === 'cr' && st.view === 'tty' && st.src === null, JSON.stringify(st));
check('…the row is gone on the reviewer', !st.row);
check('…its seeded 🔍 title is not doubled', t.some(x => x === '🔍 alpha build') && !t.some(x => /^🔍 🔍/.test(x)), JSON.stringify(t));
await page.screenshot({ path: join(HERE, 'checkprobe.png') });

// 4. the viewer moved on before the reviewer arrived → no jump
await page.evaluate(() => { focusSession(allSessions().find(s => s.cid === 'cb')); window.__sent.length = 0; });
await page.waitForTimeout(300);
await btn.tap();
await page.waitForTimeout(200);
f = await sent();
check('🔍 on beta sends check for beta', f.some(x => x.type === 'check' && x.cid === 'cb'));
await page.evaluate(() => { focusSession(allSessions().find(s => s.cid === 'ca')); });
await page.waitForTimeout(300);
const R2 = { ...R, cid: 'cr2', title: '🔍 beta work', checkOf: 'cb' };
await rx([A, B, R, R2]);
await page.waitForTimeout(400);
st = await state();
check('reviewer for beta arrives after we moved to alpha → no jump, tab in the strip',
      st.cid === 'ca' && st.src === null && (await tabs()).some(x => x === '🔍 beta work'), JSON.stringify(st));

// 5. cancel
await page.evaluate(() => { focusSession(allSessions().find(s => s.cid === 'cb')); window.__sent.length = 0; });
await page.waitForTimeout(300);
await btn.tap();
await page.waitForTimeout(200);
await rx([A, { ...B, checkArmed: true }, R, R2]);
await page.waitForTimeout(200);
st = await state();
check('armed again on beta → row up', st.row && st.src === 'cb', JSON.stringify(st));
await page.locator('#checkCancel').tap();
await page.waitForTimeout(200);
f = await sent(); st = await state();
check('cancel sends checkCancel for the open session and drops the row',
      f.some(x => x.type === 'checkCancel' && x.cid === 'cb') && !st.row && st.src === null, JSON.stringify({ f: f.map(x => x.type), st }));
await rx([A, B, R, R2, { ...R, cid: 'cr3', title: '🔍 late', checkOf: 'cb' }]);
await page.waitForTimeout(300);
st = await state();
check('a reviewer arriving after cancel never jumps', st.cid === 'cb', JSON.stringify(st));

// 6. a refusal
await page.evaluate(() => { window.__sent.length = 0; });
await btn.tap();
await page.waitForTimeout(200);
await page.evaluate(() => { window.__rx({ type: 'error', cid: 'cb', check: 'cb', error: "can't double-check: nothing to check yet — send a message first" }); });
await page.waitForTimeout(200);
st = await state();
check('a refusal → meta says why, view untouched, arm forgotten',
      /nothing to check yet/.test(st.meta) && st.cid === 'cb' && st.view === 'tty' && st.src === null, JSON.stringify(st));

// 7. a renamed reviewer still wears 🔍
await rx([A, B, { ...R, title: 'alpha review', tab: 'review' }]);
await page.waitForTimeout(200);
t = await tabs();
check('a reviewer the namer renamed still wears 🔍 on its tab', t.some(x => x === '🔍 review'), JSON.stringify(t));

check('no page errors', errors.length === 0, errors.join(' | '));
await browser.close();
console.log(failed ? 'FAIL' : 'PASS — 🔍 sends check + stays put, armed row + badge, reviewer lands, moved-on/cancel never jump, refusal, renamed badge');
process.exit(failed ? 1 : 0);
