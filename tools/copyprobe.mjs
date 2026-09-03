#!/usr/bin/env node
// copyprobe — copying a TTY selection must hand back what the user SAW, not
// what claude's TUI has since painted into those cells (2026-09-03: "I select a
// chunk, hit copy, paste gets some cut-off bit at the end and not what I had
// selected"). xterm keeps a selection as cell coordinates and getSelection()
// reads the cells at copy time; the TUI repaints its bottom rows many times a
// second, so the live read drifts. The fix snapshots the text on every
// selection-geometry change and copies the snapshot.
//
// Desktop-emulated headless Chromium against the RUNNING app. Lands on '#/p/self'
// (subscribes to nothing, claims no PTY size), forces the tty pane open, fills
// it locally with term.write, drag-selects with a REAL mouse gesture, repaints
// the selected rows, then copies two ways: Cmd+C (our key handler) and the
// browser's native copy event (Edit ▸ Copy / right-click ▸ Copy).
//
// Usage (server must be running on :8787):  cd tools && node copyprobe.mjs
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
const ctx = await browser.newContext({ viewport: { width: 1100, height: 800 } });
const page = await ctx.newPage();
page.on('pageerror', e => console.error('PAGEERROR', e.message));
await page.goto(`http://127.0.0.1:${PORT}/?t=${token}#/p/self`, { waitUntil: 'load' });
await page.waitForFunction(() => typeof term !== 'undefined' && !!term.element, null, { timeout: 10000 });
await page.waitForTimeout(1200);

const pre = await page.evaluate(() => ({ cid: currentCid, touch: window.matchMedia('(pointer: coarse)').matches }));
if (pre.cid) { console.error('ABORT: page subscribed to a real session — refusing to touch it'); await browser.close(); process.exit(2); }
if (pre.touch) { console.error('ABORT: coarse pointer — this probe needs a mouse'); await browser.close(); process.exit(2); }

// Fill the pane locally and capture what our copy path hands to the clipboard.
const geom = await page.evaluate(async () => {
  setView('tty');
  await new Promise(r => setTimeout(r, 400));
  window.__copied = [];
  window.copyToClipboard = (t) => window.__copied.push(t);   // spy: no real clipboard in headless
  for (let i = 1; i <= 20; i++) term.write(`row ${String(i).padStart(2, '0')} alpha beta gamma delta\r\n`);
  await new Promise(r => term.write('', r));
  term.focus();
  await new Promise(r => setTimeout(r, 500));   // bottomJustifyTTY translates the rows after render — measure AFTER
  const rows = document.querySelectorAll('#term .xterm-rows > div');
  const cw = term._core._renderService.dimensions.css.cell.width;
  const rowAt = (row, col) => { const r = rows[row].getBoundingClientRect(); return { x: r.left + (col + 0.5) * cw, y: r.top + r.height / 2 }; };
  return { a: rowAt(4, 0), b: rowAt(7, 30), baseY: term.buffer.active.baseY };
});
if (geom.baseY !== 0) { console.error('ABORT: buffer scrolled; row math assumes baseY 0'); await browser.close(); process.exit(2); }

// Real drag: row 5 col 0 → row 8, past the end of its text (0-based rows 4..7).
const { a, b } = geom;
await page.mouse.move(a.x, a.y);
await page.mouse.down();
await page.mouse.move(b.x, b.y, { steps: 12 });
await page.mouse.up();
await page.waitForTimeout(150);

const seen = await page.evaluate(() => term.getSelection());
const seenOK = seen.startsWith('row 05 alpha') && seen.trimEnd().endsWith('row 08 alpha beta gamma delta');
console.log('selected', JSON.stringify(seen), seenOK ? 'OK: drag selected rows 5-8' : 'FAIL: unexpected selection');

// The TUI repaints the selected rows (cursor up + rewrite, exactly Ink's move).
const live = await page.evaluate(async () => {
  let s = '\x1b[s';
  for (let r = 5; r <= 8; r++) s += `\x1b[${r};1H\x1b[2KREPAINTED ${r} ✻ Thinking… (12s · 3.1k tokens)`;
  term.write(s + '\x1b[u');
  await new Promise(r => term.write('', r));
  return term.getSelection();
});
const driftOK = live.includes('REPAINTED') && !live.includes('row 05');
console.log('live-cells', JSON.stringify(live), driftOK ? 'OK: cells under the selection changed (the trap is real)' : 'FAIL: repaint did not land');

// Cmd+C → our key handler must copy the snapshot, not the repainted cells.
await page.keyboard.press('Meta+c');
await page.waitForTimeout(100);
const copied = await page.evaluate(() => window.__copied);
const cmdOK = copied.length === 1 && copied[0] === seen;
console.log('cmd+c', JSON.stringify(copied), cmdOK ? 'OK: copied what was seen' : 'FAIL: copied drifted text');

// Native copy event (Edit ▸ Copy / right-click ▸ Copy) → same snapshot.
const native = await page.evaluate(() => {
  const ev = new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: new DataTransfer() });
  term.textarea.dispatchEvent(ev);
  return { text: ev.clipboardData.getData('text/plain'), prevented: ev.defaultPrevented };
});
const nativeOK = native.prevented && native.text === seen;
console.log('native', JSON.stringify(native), nativeOK ? 'OK: native copy carries the snapshot' : 'FAIL');

// Clearing the selection drops the snapshot: a later Cmd+C must NOT copy stale text.
const cleared = await page.evaluate(() => { term.clearSelection(); return { has: term.hasSelection(), snap: selSnap }; });
const clearOK = !cleared.has && cleared.snap === '';
console.log('cleared', JSON.stringify(cleared), clearOK ? 'OK: snapshot follows the selection' : 'FAIL: stale snapshot');

await page.screenshot({ path: join(HERE, 'copyprobe.png') });
await browser.close();
const ok = seenOK && driftOK && cmdOK && nativeOK && clearOK;
console.log(ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
