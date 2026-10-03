// checkprobe — 🔍 double-check: the OTHER engine reviews a session's work
// (2026-09-13). The harness ARMS the source (check frame), it writes a local
// REVIEW-<stamp>.md, the Stop hook spawns a reviewer (checkOf = source)
// briefed with the file + the diff; the reviewer's Stop hands the verdict back
// to the source (server-side). Guards the client half, on emulated touch with
// REAL taps (three production bugs were invisible to element.click()):
//   1. a tap on 🔍 sends {type:'check', cid} and STAYS on the source (no blank
//      terminal, no pending focus) — the meta line says the brief is coming;
//   2. checkArmed in a sessions frame → the row above the composer names the
//      reviewer engine + 🔍 on the source's tab;
//   3. the reviewer arriving (checkOf = source) → NO tab of its own: it opens
//      as the pane under the source's terminal (peek + a size claim), its
//      peekPty bytes paint the pane, the source tab wears 🔍; a REAL tap on
//      the pane's ✕ closes the reviewer only; the reviewer vanishing (it
//      closes itself after the hand-back) drops the pane + the stream;
//   4. a reviewer arriving after the viewer moved on → no jump, no tab, and
//      the pane is there on that source when you come back;
//   5. a tap on cancel sends checkCancel, drops the row, and a reviewer
//      arriving afterwards never jumps;
//   6. a refusal (error carrying check: cid) → meta says why, view untouched;
//   7. a reviewer whose source is gone is a tab again, and a renamed one
//      (namer dropped the 🔍) still wears 🔍 on it;
//   8. the severity gauge: checkLog → one dot per pass on the 🔍 chip (last
//      4), the tooltip lists every pass; a tap after a nits-only pass says
//      so in the meta line but still sends the check.
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

// 3. the reviewer arrives while we watch the source → the pane under it, no tab
const pane = () => page.evaluate(() => ({ open: !document.getElementById('peek').hidden, cid: peekCid,
  cls: document.getElementById('left').classList.contains('peekOpen'), lbl: document.getElementById('peeklbl').textContent,
  termH: document.getElementById('term').clientHeight, peekH: document.getElementById('peekterm').clientHeight,
  text: (() => { const b = peekTerm.buffer.active; let o = ''; for (let i = 0; i < b.length; i++) o += b.getLine(i).translateToString(true); return o; })() }));
await page.evaluate(() => { window.__sent.length = 0; });
const termH0 = (await pane()).termH;
await rx([{ ...A, checkArmed: false }, B, R]);
await page.waitForTimeout(500);
st = await state(); t = await tabs(); let pn = await pane(); f = await sent();
check('reviewer (checkOf = source) arrived → we stay on the source', st.cid === 'ca' && st.view === 'tty' && st.src === null, JSON.stringify(st));
check('…no tab of its own; the source tab wears 🔍', !t.some(x => /alpha build/.test(x) && x !== '🔍 alpha') && t.includes('🔍 alpha') && t.length === 2, JSON.stringify(t));
check('…the pane opens under the terminal, stacked (source keeps its width, loses rows)',
      pn.open && pn.cls && pn.cid === 'cr' && pn.peekH > 100 && pn.termH < termH0, JSON.stringify(pn) + ' was ' + termH0);
check('…names the engine', /codex/.test(pn.lbl), pn.lbl);
const termOpen = pn.termH;
check('…asks for the stream (peek cr) and claims its size', f.some(x => x.type === 'peek' && x.cid === 'cr')
      && f.some(x => x.type === 'peekResize' && x.claim === true && x.cols > 20 && x.rows > 3), JSON.stringify(f.filter(x => /^peek/.test(x.type))));
check('…and never re-subscribes the main terminal to the reviewer', !f.some(x => x.type === 'subscribe' && x.cid === 'cr'));
await page.evaluate(() => { window.__rx({ type: 'peekPty', cid: 'cr', d: btoa('codex is reading REVIEW.md') });
                            window.__rx({ type: 'peekPty', cid: 'zz', d: btoa('STRAY') }); });
await page.waitForTimeout(200);
pn = await pane();
check('peekPty bytes for the reviewer paint the pane; a stray cid never does', /codex is reading REVIEW\.md/.test(pn.text) && !/STRAY/.test(pn.text), pn.text.slice(0, 80));
check('…and never the main terminal', await page.evaluate(() => { const b = term.buffer.active; let o = ''; for (let i = 0; i < b.length; i++) o += b.getLine(i).translateToString(true); return !/codex is reading/.test(o); }));
// the tab's dot covers the reviewer: codex working in the pane = the tab pulses busy
const dotOf = (lbl) => page.evaluate((lbl) => { const b = [...document.querySelectorAll('#sessionbar .stab')].find(x => x.querySelector('.lbl')?.textContent === lbl); return b ? b.querySelector('.sdot').className : null; }, lbl);
let dc = await dotOf('🔍 alpha');
check('source idle + reviewer busy → the source tab\'s dot pulses busy', / busy/.test(dc || ''), dc);
await rx([{ ...A, checkArmed: false }, B, { ...R, busy: false }]);
await page.waitForTimeout(200);
dc = await dotOf('🔍 alpha');
check('…and goes back to idle when the reviewer stops', dc === 'sdot', dc);
await rx([{ ...A, checkArmed: false }, B, R]);
await page.waitForTimeout(200);
await page.screenshot({ path: join(HERE, 'checkprobe.png') });
await page.evaluate(() => { window.__sent.length = 0; });
await page.locator('#peekX').tap();
await page.waitForTimeout(200);
f = await sent(); st = await state();
check('a REAL tap on the pane ✕ closes the reviewer only — the source stays open', f.some(x => x.type === 'close' && x.cid === 'cr')
      && !f.some(x => x.type === 'close' && x.cid === 'ca') && st.cid === 'ca', JSON.stringify(f));
await page.evaluate(() => { window.__sent.length = 0; });
await rx([A, B]);                                   // the reviewer closed (✕, or by itself after the hand-back)
await page.waitForTimeout(300);
pn = await pane(); f = await sent();
check('the reviewer gone → the pane closes, the stream is released, the terminal gets its rows back',
      !pn.open && !pn.cls && pn.cid === null && f.some(x => x.type === 'peek' && x.cid === '') && pn.termH > termOpen + 100, JSON.stringify(pn) + ' open ' + termOpen + ' ' + JSON.stringify(f.map(x => x.type + ':' + (x.cid || ''))));

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
st = await state(); t = await tabs();
check('reviewer for beta arrives after we moved to alpha → no jump, no tab',
      st.cid === 'ca' && st.src === null && t.length === 2 && !t.some(x => /beta work/.test(x)), JSON.stringify({ st, t }));
check('…alpha shows ITS reviewer in the pane', (await pane()).cid === 'cr');
await page.evaluate(() => { window.__sent.length = 0; focusSession(allSessions().find(s => s.cid === 'cb')); });
await page.waitForTimeout(400);
pn = await pane(); f = await sent();
check('…switching to beta swaps the pane to beta\'s reviewer', pn.open && pn.cid === 'cr2' && f.some(x => x.type === 'peek' && x.cid === 'cr2'), JSON.stringify(pn));

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

// 7. source gone → the reviewer is a tab again; a renamed one still wears 🔍
await rx([B, { ...R, title: 'alpha review', tab: 'review' }]);
await page.waitForTimeout(200);
t = await tabs();
check('a reviewer whose source is gone is a tab again, renamed still wears 🔍', t.some(x => x === '🔍 review'), JSON.stringify(t));

// 8. the severity gauge
await page.evaluate(() => { focusSession(allSessions().find(s => s.cid === 'cb')); });
await page.waitForTimeout(300);
const gauge = () => page.evaluate(() => ({ text: checkBtn.textContent, tip: checkBtn.title }));
let g = await gauge();
check('no passes yet → plain 🔍, plain tooltip', g.text === '🔍' && /^Double-check/.test(g.tip), JSON.stringify(g));
const LOG = [{ worst: 'critical', counts: { critical: 1, nit: 2 } }, { worst: 'major', counts: { major: 2 } },
             { worst: 'minor', counts: { minor: 1, nit: 3 } }, { worst: 'nit', counts: { nit: 4 } }, { worst: 'nit', counts: { nit: 2 } }];
await rx([A, { ...B, checkLog: LOG }]);
await page.waitForTimeout(200);
g = await gauge();
check('checkLog → the last 4 passes as dots on the chip', g.text === '🔍🟠🟡⚪⚪', g.text);
check('…the tooltip lists every pass + says another pass is probably not worth it',
      /pass 1: critical \(1 critical, 2 nit\)/.test(g.tip) && /pass 5: nit \(2 nit\)/.test(g.tip) && /not worth it/.test(g.tip), g.tip);
await page.evaluate(() => { window.__sent.length = 0; });
await btn.tap();
await page.waitForTimeout(200);
f = await sent(); st = await state();
check('a tap after a nits-only pass still sends the check, and the meta says what the last pass found',
      f.some(x => x.type === 'check' && x.cid === 'cb') && /pass 5 found nothing critical or major/.test(st.meta), st.meta);
await rx([A, { ...B, checkLog: [{ worst: 'major', counts: { major: 1 } }] }]);
await page.waitForTimeout(200);
g = await gauge();
check('a major pass → 🟠, tooltip says worth another pass', g.text === '🔍🟠' && /worth another pass/.test(g.tip), JSON.stringify(g));
await page.evaluate(() => { focusSession(allSessions().find(s => s.cid === 'ca')); });
await page.waitForTimeout(300);
check('another session without a log → plain 🔍 again', (await gauge()).text === '🔍');

check('no page errors', errors.length === 0, errors.join(' | '));
await browser.close();
console.log(failed ? 'FAIL' : 'PASS — 🔍 sends check + stays put, armed row + badge, reviewer pane under the source (no tab, own stream + size, ✕ = reviewer only, closes with it), moved-on/cancel never jump, refusal, orphan tab badge, severity gauge');
process.exit(failed ? 1 : 0);
