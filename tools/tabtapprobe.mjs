#!/usr/bin/env node
// tabtapprobe — guards "I click a tab and nothing happens" (Austin, 2026-11-09).
// The tab strip repaints on every sessions/projects frame and roster pulse,
// several times a second. It used to delete and recreate every tab each time,
// so a click whose press landed on the old node and whose release landed on
// its replacement never fired. Tabs are now reconciled by cid (landmine 4).
//
// Lands on the sessions rung (`#/p/self`), never on a session. hsend/hsendTo
// are stubbed and focusSession is captured, so nothing subscribes to, resizes
// or types into a real session.
//
// Asserts:
//   1. a repaint keeps every tab NODE (same element, not a look-alike);
//   2. REAL mouse: press on a tab, repaints land while the button is down,
//      release → the tab's session opens;
//   3. REAL touch (second, phone-shaped page): touchstart, repaints, touchend
//      → the tab's session opens;
//   4. a changed title is written into the SAME node;
//   5. a reordered roster moves the nodes instead of rebuilding them;
//   6. a session that leaves the roster loses its tab, the rest keep theirs.
//
// Usage (server must be running on :8787):  cd tools && node tabtapprobe.mjs

import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const PORT = process.env.HARNESS_PORT || '8787';

function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  if (!existsSync(cache)) return null;
  const shells = readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of shells)
    for (const arch of ['mac-arm64', 'mac-x64']) {
      const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  return null;
}

const exec = findChromium();
if (!exec) { console.error('No cached playwright chromium found. Run: cd tools && npx playwright install chromium'); process.exit(2); }

let token = '';
try { token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim(); } catch {}
const url = `http://127.0.0.1:${PORT}/?t=${token}#/p/self`;

const browser = await chromium.launch({ executablePath: exec });
let failed = false;
const fail = m => { console.error('FAIL: ' + m); failed = true; };
const pass = m => console.log('PASS: ' + m);
const check = (name, ok, detail) => ok ? pass(name) : fail(name + (detail ? ' — ' + detail : ''));

// A page with the wire cut: frames we'd send are captured, focusSession records.
async function open(ctxOpts) {
  const ctx = await browser.newContext(ctxOpts);
  const page = await ctx.newPage();
  try {
    await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
  } catch (e) {
    console.error(`Could not load ${url} — is server.py running on :${PORT}?  (${e.message})`);
    await browser.close(); process.exit(2);
  }
  await page.waitForTimeout(2000);
  await page.evaluate(() => {
    window.__sent = [];
    window.hsend = f => { window.__sent.push(f); return true; };
    window.hsendTo = (m, f) => { window.__sent.push(f); return true; };
    window.__opened = [];
    window.__realFocus = window.focusSession;
    window.focusSession = s => { window.__opened.push(s && s.cid); };
  });
  return page;
}
const tabCids = page => page.evaluate(() => [...document.querySelectorAll('#sessionbar .stab')].map(t => t.dataset.cid));
// what a burst of server frames does to the strip
const storm = (page, n = 6) => page.evaluate(n => { for (let i = 0; i < n; i++) renderSessionBar(); }, n);

// ───────────── desktop ─────────────
const page = await open({ viewport: { width: 1200, height: 800 } });
const before = await tabCids(page);
if (before.length < 2) { console.log('SKIP: fewer than two tabs on this harness'); await browser.close(); process.exit(0); }

// ---- 1: a repaint keeps every node ----
const kept = await page.evaluate(() => {
  const nodes = [...document.querySelectorAll('#sessionbar .stab')];
  for (let i = 0; i < 5; i++) renderSessionBar();
  const now = [...document.querySelectorAll('#sessionbar .stab')];
  return { n: nodes.length, same: nodes.length === now.length && nodes.every((x, i) => x === now[i]),
           filterLast: document.getElementById('sessionbar').lastElementChild.classList.contains('tfilter') };
});
check('a repaint keeps every tab node', kept.same, JSON.stringify(kept));
check('the 🔎 box stays last on the strip', kept.filterLast);

// ---- 2: mouse press → frames land → release ----
const target = before[1];
const box = await page.locator(`#sessionbar .stab[data-cid="${target}"]`).boundingBox();
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
await page.mouse.down();
await storm(page);                       // frames arrive while the button is held
await page.waitForTimeout(40);
await storm(page);
await page.mouse.up();
await page.waitForTimeout(100);
const opened = await page.evaluate(() => window.__opened.slice());
check('mouse: a click that spans repaints opens that tab', opened.length === 1 && opened[0] === target, 'opened=' + JSON.stringify(opened));

// ---- 4: a changed title lands in the same node ----
const retitled = await page.evaluate(cid => {
  const node = document.querySelector(`#sessionbar .stab[data-cid="${cid}"]`);
  const s = sessionList.find(x => x.cid === cid);          // allSessions() hands out copies
  if (!s) return { skip: true };
  const was = { tab: s.tab, title: s.title };
  s.tab = ''; s.title = 'tabtapprobe-retitled';
  renderSessionBar();
  const now = document.querySelector(`#sessionbar .stab[data-cid="${cid}"]`);
  const out = { same: now === node, text: now.querySelector('.lbl').textContent };
  s.tab = was.tab; s.title = was.title; renderSessionBar();
  return out;
}, target);
if (retitled.skip) console.log('SKIP: title check (session not on this machine\'s list)');
else check('a new title is written into the same node', retitled.same && retitled.text.includes('tabtapprobe-retitled'), JSON.stringify(retitled));

// ---- 5 + 6: reorder moves nodes; a departed session loses only its own tab ----
const moved = await page.evaluate(() => {
  const nodes = new Map([...document.querySelectorAll('#sessionbar .stab')].map(n => [n.dataset.cid, n]));
  const orig = window.railSessions;
  const rev = () => orig().slice().reverse();
  window.railSessions = rev;
  let out;
  try {
    renderSessionBar();
    const now = [...document.querySelectorAll('#sessionbar .stab')];
    const want = rev().map(s => s.cid);
    out = { ordered: now.map(n => n.dataset.cid).join() === want.join(),
            sameNodes: now.every(n => nodes.get(n.dataset.cid) === n) };
    const gone = want[0];
    window.railSessions = () => rev().slice(1);
    renderSessionBar();
    const after = [...document.querySelectorAll('#sessionbar .stab')];
    out.dropped = !after.some(n => n.dataset.cid === gone) && after.length === want.length - 1;
    out.restKept = after.every(n => nodes.get(n.dataset.cid) === n);
  } finally { window.railSessions = orig; renderSessionBar(); }
  out.restored = [...document.querySelectorAll('#sessionbar .stab')].length === nodes.size;
  return out;
});
check('a reordered roster reorders the tabs', moved.ordered, JSON.stringify(moved));
check('…by moving the existing nodes', moved.sameNodes);
check('a session that left loses its tab', moved.dropped);
check('…and the other tabs keep their nodes', moved.restKept && moved.restored);

// ───────────── the sessions rung (same wipe, same lost taps) ─────────────
// `#/p/self` IS the sessions rung. openSession is captured; the ✕ goes through
// the stubbed hsend, so a real session is never opened or closed.
await page.evaluate(() => { window.__opened = []; window.openSession = cid => { window.__opened.push(cid); }; });
const rstorm = (n = 6) => page.evaluate(n => { for (let i = 0; i < n; i++) renderSessions(); }, n);
const cardCids = await page.evaluate(() => [...document.querySelectorAll('#sessions > .scard')].map(c => c.dataset.cid));
if (cardCids.length < 1) console.log('SKIP: no session cards on this rung');
else {
  const keptC = await page.evaluate(() => {
    const grab = () => [...document.querySelectorAll('#sessions > .scard, #sessions .sclose, #newSession, #sessions .menutitle')];
    const nodes = grab();
    for (let i = 0; i < 5; i++) renderSessions();
    const now = grab();
    return { n: nodes.length, same: nodes.length === now.length && nodes.every((x, i) => x === now[i]) };
  });
  check('rung: a repaint keeps every card, ✕, title and ＋ node', keptC.same, JSON.stringify(keptC));
  const cc = cardCids[cardCids.length - 1];
  const tb = await page.locator(`#sessions > .scard[data-cid="${cc}"] .stitle`).boundingBox();
  await page.mouse.move(tb.x + Math.min(20, tb.width / 2), tb.y + tb.height / 2);
  await page.mouse.down();
  await rstorm(); await page.waitForTimeout(40); await rstorm();
  await page.mouse.up();
  await page.waitForTimeout(100);
  const co = await page.evaluate(() => window.__opened.slice());
  check('rung: a card click that spans repaints opens that session', co.length === 1 && co[0] === cc, 'opened=' + JSON.stringify(co));
  // a live change lands in the SAME card
  const live = await page.evaluate(cid => {
    const node = document.querySelector(`#sessions > .scard[data-cid="${cid}"]`);
    const s = sessionList.find(x => x.cid === cid);
    if (!s) return { skip: true };
    const was = { busy: s.busy, tool: s.tool, blocked_on: s.blocked_on, alive: s.alive, waiting: s.waiting };
    s.alive = true; s.waiting = false; s.busy = true; s.tool = 'TabTapTool'; s.blocked_on = 'probe question?';
    renderSessions();
    const now = document.querySelector(`#sessions > .scard[data-cid="${cid}"]`);
    const out = { same: now === node, meta: now.querySelector('.smeta').textContent, blk: (now.querySelector('.sblock') || {}).textContent || '',
                  order: [...now.children].map(c => c.className.split(' ')[0]).join() };
    Object.assign(s, was); renderSessions();
    out.cleared = !document.querySelector(`#sessions > .scard[data-cid="${cid}"] .sblock`) || !!was.blocked_on;
    return out;
  }, cc);
  if (live.skip) console.log('SKIP: live-change check (session not on this machine\'s list)');
  else {
    check('rung: a live change is written into the same card', live.same && /running TabTapTool/.test(live.meta) && /probe question/.test(live.blk), JSON.stringify(live));
    check('rung: the card keeps its line order (…, needs-you, meta, ✕)', /sblock,smeta,sclose$/.test(live.order), live.order);
    check('rung: a line that no longer applies is removed', live.cleared);
  }
  // ✕ pressed, repaints land, released → exactly one close frame, captured not sent
  await page.evaluate(() => { window.__sent = []; });
  const xb = await page.locator(`#sessions > .scard[data-cid="${cc}"] .sclose`).boundingBox();
  await page.mouse.move(xb.x + xb.width / 2, xb.y + xb.height / 2);
  await page.mouse.down();
  await rstorm(); await page.waitForTimeout(40); await rstorm();
  await page.mouse.up();
  await page.waitForTimeout(100);
  const closes = await page.evaluate(() => window.__sent.filter(f => f.type === 'close').map(f => f.cid));
  check('rung: a ✕ click that spans repaints asks to close that session (captured, never sent)', closes.length === 1 && closes[0] === cc, 'close=' + JSON.stringify(closes));
  const co2 = await page.evaluate(() => window.__opened.length);
  check('rung: …and the ✕ did not also open the card', co2 === 1, 'opened=' + co2);
  await page.evaluate(() => { window.__sent = window.__sent.filter(f => f.type !== 'close'); });
}

const sent = await page.evaluate(() => window.__sent.length);
check('nothing was sent to the server', sent === 0, 'frames=' + sent);

// ───────────── the top-right pill and its 🔑 sign-in button ─────────────
// A FAKE session (never on the server) is opened so the pill has something to
// show; live frames are held off for these few lines so they can't drop it.
const PCID = '00000000-probe-tabtap-pill';
const pill = await page.evaluate(PCID => {
  window.__hj = window.handleJson; window.handleJson = () => {};
  window.__added = []; window.addAccount = (mid, name) => { window.__added.push(name); };
  const fake = { cid: PCID, pid: 'self', title: 'probe pill session', tab: 'pill', promptCount: 2, alive: true, busy: false,
                 model: 'claude-opus-5', ctxTokens: 120000, lastActive: Date.now() / 1000, promptedAt: Date.now() / 1000,
                 loginCta: { name: 'sub9', reason: 'probe' } };
  sessionList.push(fake);
  window.__realFocus(sessionList.find(s => s.cid === PCID));
  refreshSessionTag();
  const b = document.querySelector('#machineTag .cta');
  if (!b) return { noCta: true, view: currentView(), html: document.getElementById('machineTag').innerHTML.slice(0, 200) };
  const r = b.getBoundingClientRect();
  window.__ctaNode = b;
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, text: b.textContent, hit: document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) === b };
}, PCID);
if (pill.noCta) fail('pill: no 🔑 sign-in button for a session with a loginCta — ' + JSON.stringify(pill));
else {
  check('pill: the 🔑 sign-in button is up and reachable', /sign in to/.test(pill.text) && pill.hit, JSON.stringify(pill));
  await page.mouse.move(pill.x, pill.y);
  await page.mouse.down();
  await page.evaluate(() => { for (let i = 0; i < 6; i++) refreshSessionTag(); });   // sessions/accounts frames
  await page.waitForTimeout(40);
  await page.evaluate(() => { for (let i = 0; i < 6; i++) refreshSessionTag(); });
  await page.mouse.up();
  await page.waitForTimeout(100);
  const pr = await page.evaluate(PCID => {
    const out = { added: window.__added.slice(), same: document.querySelector('#machineTag .cta') === window.__ctaNode };
    const s = sessionList.find(x => x.cid === PCID);
    s.model = 'claude-haiku-5-5'; refreshSessionTag();       // a real change still repaints
    out.model = (document.querySelector('#machineTag .model') || {}).textContent || '';
    return out;
  }, PCID);
  check('pill: repaints keep the 🔑 button node', pr.same);
  check('pill: a click that spans repaints starts that sign-in', pr.added.length === 1 && pr.added[0] === 'sub9', 'added=' + JSON.stringify(pr.added));
  check('pill: a changed model still updates it', /haiku/i.test(pr.model), 'model=' + pr.model);
}
const psent0 = await page.evaluate(() => window.__sent.filter(f => f.type !== 'subscribe' && f.type !== 'resize' && f.type !== 'unsubscribe' && f.type !== 'tldr' && f.type !== 'stt').map(f => f.type));
check('pill: nothing but the fake session\'s own view frames were captured', psent0.length === 0, 'frames=' + JSON.stringify(psent0));
await page.context().close();

// ───────────── touch ─────────────
const phone = await open({ viewport: { width: 430, height: 860 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
const ptabs = await tabCids(phone);
if (ptabs.length < 1) console.log('SKIP: no tabs on the phone-shaped page');
else {
  const pt = ptabs[0];
  await phone.evaluate(cid => document.querySelector(`#sessionbar .stab[data-cid="${cid}"]`).scrollIntoView({ inline: 'center' }), pt);
  const b = await phone.locator(`#sessionbar .stab[data-cid="${pt}"]`).boundingBox();
  const x = Math.round(b.x + b.width / 2), y = Math.round(b.y + b.height / 2);
  const cdp = await phone.context().newCDPSession(phone);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
  await storm(phone);                    // frames arrive while the finger is down
  await phone.waitForTimeout(40);
  await storm(phone);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await phone.waitForTimeout(250);
  const po = await phone.evaluate(() => window.__opened.slice());
  check('touch: a tap that spans repaints opens that tab', po.length === 1 && po[0] === pt, 'opened=' + JSON.stringify(po));
  const psent = await phone.evaluate(() => window.__sent.length);
  check('touch: nothing was sent to the server', psent === 0, 'frames=' + psent);
}

await browser.close();
console.log(failed ? 'tabtapprobe: FAIL' : 'tabtapprobe: all green');
process.exit(failed ? 1 : 0);
