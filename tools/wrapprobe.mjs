// wrapprobe — 📑 wrap: the doc chip that closes the tab itself (2026-09-06).
// The harness ARMS a session (wrap frame), claude writes the handoff and runs
// `harness-close`, the Stop hook closes it, and the tab vanishes ON PURPOSE.
// Guards the client half, on emulated touch with REAL taps (three production
// bugs were invisible to element.click()):
//   1. a tap on 📑 sends {type:'wrap', cid, text} (not a plain send) + a pending chip;
//   2. wrapArmed in a sessions frame → the row above the composer + 📑 on the tab;
//   3. a tap on cancel sends wrapCancel and drops the row;
//   4. wrapClosing → the row says so;
//   5. the closing cid vanishing from the roster → land on the rail neighbour
//      (no dead veil, no black tty) + the "wrapped up · ↩" toast;
//   6. a tap on ↩ sends reopen {cid} (the 🗃️ path) and the toast goes;
//   7. a NON-wrapping session vanishing never toasts (that's the dead veil's job).
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

const A = { cid: 'wa', pid: 'p1', title: 'alpha handoff', tab: 'alpha', alive: true, busy: false, pinned: 0, promptedAt: 1, lastActive: 2, engine: 'claude' };
const B = { cid: 'wb', pid: 'p1', title: 'beta work', tab: 'beta', alive: true, busy: false, pinned: 0, promptedAt: 1, lastActive: 1, engine: 'claude' };
const sent = () => page.evaluate(() => window.__sent.map(x => { try { return JSON.parse(x); } catch { return null; } }).filter(Boolean));
await page.evaluate(({ A, B }) => {
  window.__rx({ type: 'projects', projects: [{ pid: 'p1', name: 'alpha', repoUrl: 'https://github.com/x/alpha', kind: 'gh', status: 'ready', sessionCount: 2, busyCount: 0, waitingCount: 0, created: 1, pinned: false, lastTouched: 10, emoji: '' }], boot: 'b1' });
  window.__rx({ type: 'sessions', sessions: [A, B], current: null });
  window.__rx({ type: 'irons', irons: [] });
  window.__rx({ type: 'closed', closed: [] });
}, { A, B });
await page.waitForTimeout(300);
await page.evaluate(() => { focusSession(allSessions().find(s => s.cid === 'wa')); window.__sent.length = 0; });
await page.waitForTimeout(400);
const v0 = await page.evaluate(() => ({ view: currentView(), cid: currentCid }));
check('a tap on the alpha tab opens its tty', v0.view === 'tty' && v0.cid === 'wa', JSON.stringify(v0));

// 1. a REAL tap on the 📑 chip
const chip = page.locator('#quickchips button', { hasText: '📑' });
await chip.scrollIntoViewIfNeeded();
await chip.tap();
await page.waitForTimeout(300);
let f = await sent();
const wrap = f.find(x => x.type === 'wrap');
check('📑 sends a wrap frame for the open session with the handoff text',
      !!wrap && wrap.cid === 'wa' && /harness-close/.test(wrap.text) && /do NOT close/.test(wrap.text), JSON.stringify(f.map(x => x.type)));
check('…and NOT a plain send', !f.some(x => x.type === 'send'));
check('…with a pending chip in the composer', await page.evaluate(() => [...document.querySelectorAll('.pending-msg')].some(d => /wrapping this session up/.test(d.textContent))));

// 2. armed → the row + the tab badge
await page.evaluate(({ A, B }) => { window.__rx({ type: 'sessions', sessions: [{ ...A, wrapArmed: true }, B] }); }, { A, B });
await page.waitForTimeout(200);
const armed = await page.evaluate(() => ({
  row: !document.getElementById('wraprow').hidden, text: document.getElementById('wraptext').textContent,
  tab: [...document.querySelectorAll('#sessionbar .stab')].map(t => t.querySelector('.lbl').textContent) }));
check('wrapArmed → the 📑 row above the composer says it will close itself', armed.row && /closes itself/.test(armed.text), JSON.stringify(armed));
check('…and the tab wears 📑', armed.tab.some(t => t.startsWith('📑 alpha')), JSON.stringify(armed.tab));

// 3. a REAL tap on cancel
await page.evaluate(() => { window.__sent.length = 0; });
await page.locator('#wrapCancel').tap();
await page.waitForTimeout(200);
f = await sent();
check('cancel sends wrapCancel for the open session and drops the row',
      f.some(x => x.type === 'wrapCancel' && x.cid === 'wa') && await page.evaluate(() => document.getElementById('wraprow').hidden), JSON.stringify(f));

// 4. closing → the row says so
await page.evaluate(({ A, B }) => { window.__rx({ type: 'sessions', sessions: [{ ...A, wrapArmed: true, wrapClosing: true }, B] }); }, { A, B });
await page.waitForTimeout(200);
const closing = await page.evaluate(() => ({ row: !document.getElementById('wraprow').hidden, text: document.getElementById('wraptext').textContent }));
check('wrapClosing → "closing when this turn ends"', closing.row && /closing when this turn ends/.test(closing.text), JSON.stringify(closing));

// 5. the closing cid vanishes → land on beta, toast up, no veil
await page.evaluate(({ B }) => { window.__sent.length = 0; window.__rx({ type: 'sessions', sessions: [B] }); }, { B });
await page.waitForTimeout(400);
const landed = await page.evaluate(() => ({
  cid: currentCid, view: currentView(), veil: document.getElementById('deadveil').hidden,
  toast: !document.getElementById('wraptoast').hidden, toastText: document.getElementById('wraptoast').textContent,
  row: document.getElementById('wraprow').hidden }));
check('the wrapping tab vanished → landed on the rail neighbour, tty, no dead veil',
      landed.cid === 'wb' && landed.view === 'tty' && landed.veil && landed.row, JSON.stringify(landed));
check('…and the "wrapped up" toast names it with ↩', landed.toast && /alpha wrapped up/.test(landed.toastText) && /↩/.test(landed.toastText), JSON.stringify(landed));
await page.screenshot({ path: join(HERE, 'wrapprobe.png') });

// 6. a REAL tap on ↩
await page.locator('#wraptoast .wt-undo').tap();
await page.waitForTimeout(300);
f = await sent();
const undo = await page.evaluate(() => ({ toast: document.getElementById('wraptoast').hidden, pending: pendingNewFocus === true }));
check('↩ sends reopen for the wrapped cid and awaits the focus', f.some(x => x.type === 'reopen' && x.cid === 'wa') && undo.toast && undo.pending, JSON.stringify({ f, undo }));
await page.evaluate(() => { pendingNewFocus = false; clearNewFocusWatch(); hideDeadVeil(); });

// 7. a plain close never toasts
await page.evaluate(({ B }) => { window.__rx({ type: 'sessions', sessions: [B] }); focusSession(allSessions().find(s => s.cid === 'wb')); window.__rx({ type: 'sessions', sessions: [] }); }, { B });
await page.waitForTimeout(300);
const plain = await page.evaluate(() => ({ toast: document.getElementById('wraptoast').hidden }));
check('a session that was NOT wrapping vanishes → no toast (the dead veil owns that)', plain.toast, JSON.stringify(plain));

check('no page errors', errors.length === 0, errors.join(' | '));
await browser.close();
console.log(failed ? 'FAIL' : 'PASS — 📑 sends wrap, armed row + badge, cancel, vanish lands + toast, ↩ reopens, plain close silent');
process.exit(failed ? 1 : 0);
