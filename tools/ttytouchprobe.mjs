#!/usr/bin/env node
// ttytouchprobe — on touch the xterm text layer is click-through (so a finger
// can scroll), which left links untappable and nothing copyable (Austin,
// 11-25). Checks the two paths built on the buffer instead:
//   tap        → the URL under the finger opens (short, hard-wrapped, soft-wrapped)
//   long-press → #ttytext shows the terminal's text as selectable text with links
// and that a tap on plain text opens nothing and a scroll drag opens no view.
//
// Touch-emulated headless Chromium against the RUNNING app, on '#/p/self'
// (subscribes to nothing). Real taps, never .click().
// Usage (server must be running on :8787):  cd tools && node ttytouchprobe.mjs
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

const browser = await chromium.launch({ executablePath: exec });
const ctx = await browser.newContext({ viewport: { width: 390, height: 780 }, hasTouch: true, isMobile: true });
const page = await ctx.newPage();
page.on('pageerror', e => console.error('PAGEERROR', e.message));
await page.goto(`http://127.0.0.1:${PORT}/?t=${token}#/p/self`, { waitUntil: 'load' });
await page.waitForFunction(() => typeof term !== 'undefined' && !!term.element, null, { timeout: 10000 });
await page.waitForTimeout(1200);

const pre = await page.evaluate(() => ({ cid: currentCid, touch: window.matchMedia('(pointer: coarse)').matches }));
if (pre.cid) { console.error('ABORT: page subscribed to a real session — refusing to touch it'); await browser.close(); process.exit(2); }
if (!pre.touch) { console.error('ABORT: fine pointer — this probe needs touch emulation'); await browser.close(); process.exit(2); }

let fails = 0;
const check = (name, ok, detail) => { if (!ok) fails++; console.log(name, JSON.stringify(detail), ok ? 'OK' : 'FAIL'); };

const g = await page.evaluate(async () => {
  setView('tty');
  await new Promise(r => setTimeout(r, 400));
  window.__opened = [];
  window.open = (u) => { window.__opened.push(u); return null; };
  const cols = term.cols;
  const hard = ('https://example.com/hard/' + 'a'.repeat(200)).slice(0, cols);   // exactly one full row
  const soft = 'https://example.com/soft/' + 'b'.repeat(cols);                   // xterm wraps it itself
  term.write('plain words only here\r\n');                       // row 0
  term.write('see https://example.com/short. ok\r\n');           // row 1
  term.write(hard + '\r\n');                                     // row 2
  term.write('  tail-1/end\r\n');                                // row 3: claude-style hard wrap + indent
  term.write(soft + '\r\n');                                     // rows 4-5
  term.write('last line\r\n');
  await new Promise(r => term.write('', r));
  await new Promise(r => setTimeout(r, 500));                    // bottomJustifyTTY moves the rows after render
  const scr = document.querySelector('#term .xterm-screen').getBoundingClientRect();
  const cw = scr.width / term.cols, ch = scr.height / term.rows;
  const at = (row, col) => ({ x: scr.left + (col + 0.5) * cw, y: scr.top + (row + 0.5) * ch });
  return { cols, hard, soft, baseY: term.buffer.active.baseY,
           plain: at(0, 3), short: at(1, 12), hardTop: at(2, 10), hardTail: at(3, 5), softTail: at(5, 2),
           through: getComputedStyle(document.querySelector('#term .xterm-screen')).pointerEvents };
});
if (g.baseY !== 0) { console.error('ABORT: buffer scrolled; row math assumes baseY 0'); await browser.close(); process.exit(2); }
check('text layer still click-through (scroll path untouched)', g.through === 'none', g.through);

const tap = async (pt) => {
  await page.evaluate(() => { window.__opened.length = 0; });
  await page.touchscreen.tap(pt.x, pt.y);
  await page.waitForTimeout(150);
  return page.evaluate(() => window.__opened.slice());
};
let o = await tap(g.plain);
check('tap plain text opens nothing', o.length === 0, o);
o = await tap(g.short);
check('tap short link (trailing "." dropped)', o.length === 1 && o[0] === 'https://example.com/short', o);
const hardFull = g.hard + 'tail-1/end';
o = await tap(g.hardTop);
check('tap hard-wrapped link, first row', o.length === 1 && o[0] === hardFull, o);
o = await tap(g.hardTail);
check('tap hard-wrapped link, second row', o.length === 1 && o[0] === hardFull, o);
o = await tap(g.softTail);
check('tap soft-wrapped link, second row', o.length === 1 && o[0] === g.soft, o);

// long-press (real touch events over CDP) → the text view
const cdp = await ctx.newCDPSession(page);
const press = async (pt, ms, moveBy) => {
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: pt.x, y: pt.y }] });
  if (moveBy) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: pt.x, y: pt.y + moveBy }] });
  await page.waitForTimeout(ms);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitForTimeout(150);
};
await press(g.plain, 800, 40);
let open = await page.evaluate(() => !document.getElementById('ttytext').hidden);
check('a held scroll drag opens no text view', open === false, open);
await press(g.plain, 250);
open = await page.evaluate(() => !document.getElementById('ttytext').hidden);
check('a short tap opens no text view', open === false, open);
await press(g.plain, 800);
const view = await page.evaluate(() => {
  const box = document.getElementById('ttytext'), body = document.getElementById('ttytextBody');
  const t = document.getElementById('term').getBoundingClientRect(), b = box.getBoundingClientRect();
  return { open: !box.hidden, text: body.textContent,
           links: [...body.querySelectorAll('a')].map(a => a.getAttribute('href')),
           select: getComputedStyle(body).userSelect,
           covers: Math.abs(t.top - b.top) < 2 && Math.abs(t.height - b.height) < 2 };
});
check('long-press opens the text view over the terminal', view.open && view.covers, { open: view.open, covers: view.covers });
check('its text is selectable', view.select === 'text', view.select);
check('soft-wrapped row is one unbroken line in the copy', view.text.includes(g.soft + '\nlast line'), view.text.length);
check('plain rows are there', view.text.startsWith('plain words only here\nsee https://example.com/short. ok\n'), view.text.slice(0, 60));
const wantLinks = ['https://example.com/short', hardFull, hardFull, g.soft, g.soft];   // one anchor per row a URL touches
check('links are real anchors with whole URLs', JSON.stringify(view.links) === JSON.stringify(wantLinks), view.links.map(l => l.length));

await page.evaluate(() => { window.__copied = []; window.copyToClipboard = (t) => window.__copied.push(t); });
const allBtn = await page.evaluate(() => { const r = document.getElementById('ttytextAll').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
await page.touchscreen.tap(allBtn.x, allBtn.y);
await page.waitForTimeout(100);
const copied = await page.evaluate(() => window.__copied);
check('📋 copy all hands over the whole text', copied.length === 1 && copied[0] === view.text, copied.length);

const xBtn = await page.evaluate(() => { const r = document.getElementById('ttytextX').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
await page.touchscreen.tap(xBtn.x, xBtn.y);
await page.waitForTimeout(100);
open = await page.evaluate(() => !document.getElementById('ttytext').hidden);
check('✕ returns to the terminal', open === false, open);

await page.screenshot({ path: join(HERE, 'ttytouchprobe.png') });
await browser.close();
console.log(fails ? `\n${fails} FAILED` : '\nALL OK');
process.exit(fails ? 1 : 0);
