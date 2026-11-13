#!/usr/bin/env node
// taskprobe — guards the ☰ session task chip (2026-11-12): the high/low counts
// under the tty's corner buttons and the panel a tap opens.
// Never touches a real session: the page's socket handler is detached, hsend
// is stubbed (every frame captured), and the "open session" is a fake row.
//
// Asserts, on desktop (real clicks) and on an emulated phone (real taps):
//   1. no list → no chip; a list → the chip shows "N high", "N low" and sits
//      under the corner buttons, inside the pane;
//   2. a tap opens the panel: open high first, then low, then done (struck);
//   3. ☐ sends stask done, the level sends stask lev (flipped), ✕ sends stask rm;
//   4. a repaint (what every `sessions` frame does) keeps the row NODES;
//   5. everything open done → the chip says "all done".
//
// Usage (server must be running on :8787):  cd tools && node taskprobe.mjs

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

async function run(label, ctxOpts, touch) {
  const ctx = await browser.newContext(ctxOpts);
  const page = await ctx.newPage();
  try { await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 }); }
  catch (e) { console.error(`Could not load ${url} — is server.py running on :${PORT}?  (${e.message})`); await browser.close(); process.exit(2); }
  await page.waitForTimeout(1500);
  const hit = async sel => {
    const b = await page.locator(sel).first().boundingBox();
    if (touch) await page.touchscreen.tap(b.x + b.width / 2, b.y + b.height / 2);
    else await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
    await page.waitForTimeout(120);
  };
  // a fake open session; no frame in, no frame out
  await page.evaluate(() => {
    if (ws) ws.onmessage = null;
    window.__sent = [];
    window.hsend = f => { window.__sent.push(f); return true; };
    window.currentView = () => 'tty';
    window.__fake = { cid: 'fake-task-probe', pid: 'self', title: 'probe' };
    sessionList = [window.__fake]; currentCid = window.__fake.cid;
    for (const id of ['closeBtn', 'pinBtn', 'machineTag']) document.getElementById(id).classList.add('show');
    renderTasks();
  });
  const chip = () => page.evaluate(() => {
    const c = document.getElementById('taskChip'), r = c.getBoundingClientRect();
    const x = document.getElementById('closeBtn').getBoundingClientRect(), m = document.querySelector('main').getBoundingClientRect();
    return { shown: getComputedStyle(c).display !== 'none', text: c.textContent, top: r.top, right: r.right, left: r.left,
             closeBottom: x.bottom, mainRight: m.right, mainLeft: m.left };
  });
  let c = await chip();
  if (!c.shown) pass(`${label}: no list → no chip`); else fail(`${label}: chip shown with no list`);

  const setTasks = items => page.evaluate(items => {
    const open = items.filter(i => !i.done);
    window.__fake.tasks = { hi: open.filter(i => i.lev === 'hi').length, lo: open.filter(i => i.lev === 'lo').length,
                            done: items.length - open.length, at: Date.now() / 1000 - 7200, items };
    renderTasks();
  }, items);
  const list = [
    { id: 'a1', text: 'wire the stale reminder into the prompt hook', lev: 'hi', done: false },
    { id: 'a2', text: 'show the counts on the tab strip', lev: 'hi', done: false },
    { id: 'b1', text: 'tidy the panel css', lev: 'lo', done: false },
    { id: 'd1', text: 'write the CLI', lev: 'hi', done: true },
  ];
  await setTasks(list);
  c = await chip();
  if (c.shown && c.text.includes('2 high') && c.text.includes('1 low') && c.text.includes('2h')
      && c.top >= c.closeBottom && c.right <= c.mainRight + 1 && c.left >= c.mainLeft)
    pass(`${label}: chip "${c.text}" under the corner buttons`);
  else fail(`${label}: chip ` + JSON.stringify(c));

  await hit('#taskChip');
  const rows = () => page.evaluate(() => [...document.querySelectorAll('#taskPanel .tki')].map(n =>
    ({ id: n.dataset.id, done: n.classList.contains('done'), lev: n.querySelector('.tklev').textContent })));
  let r = await rows();
  const vis = await page.evaluate(() => { const p = document.getElementById('taskPanel'), b = p.getBoundingClientRect();
    return { hidden: p.hidden, left: b.left, right: b.right, w: innerWidth }; });
  if (!vis.hidden && vis.left >= 0 && vis.right <= vis.w && r.map(x => x.id).join() === 'a1,a2,b1,d1' && r[3].done && r[2].lev === 'low' && r[0].lev === 'HIGH')
    pass(`${label}: a tap opens the panel, high → low → done`);
  else fail(`${label}: panel ` + JSON.stringify({ vis, r }));
  await page.screenshot({ path: join(HERE, `taskprobe-${label}.png`) });

  await page.evaluate(() => { for (const n of document.querySelectorAll('#taskPanel .tki')) n.__probe = 1; });
  await hit('#taskPanel .tki[data-id="a1"] .tkchk');
  await hit('#taskPanel .tki[data-id="b1"] .tklev');
  await hit('#taskPanel .tki[data-id="a2"] .tkrm');
  const sent = await page.evaluate(() => window.__sent);
  const want = JSON.stringify([
    { type: 'stask', cid: 'fake-task-probe', op: 'done', id: 'a1' },
    { type: 'stask', cid: 'fake-task-probe', op: 'lev', id: 'b1', lev: 'hi' },
    { type: 'stask', cid: 'fake-task-probe', op: 'rm', id: 'a2' }]);
  if (JSON.stringify(sent) === want) pass(`${label}: ☐ / level / ✕ send done / lev / rm`);
  else fail(`${label}: sent ` + JSON.stringify(sent));

  // the server's answer: a1 done, b1 high, a2 gone
  await setTasks([{ ...list[2], lev: 'hi' }, { ...list[0], done: true }, list[3]]);
  const kept = await page.evaluate(() => [...document.querySelectorAll('#taskPanel .tki')].map(n => [n.dataset.id, !!n.__probe]));
  if (JSON.stringify(kept) === JSON.stringify([['b1', true], ['a1', true], ['d1', true]])) pass(`${label}: a repaint keeps the row nodes, reordered`);
  else fail(`${label}: repaint ` + JSON.stringify(kept));

  await setTasks([{ ...list[0], done: true }, list[3]]);
  c = await chip();
  if (c.shown && c.text.includes('all done')) pass(`${label}: nothing open → "${c.text}"`);
  else fail(`${label}: done chip ` + JSON.stringify(c));
  await ctx.close();
}

await run('desktop', { viewport: { width: 1100, height: 760 } }, false);
await run('phone', { viewport: { width: 390, height: 780 }, hasTouch: true, isMobile: true }, true);
await browser.close();
process.exit(failed ? 1 : 0);
