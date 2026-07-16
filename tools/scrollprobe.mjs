#!/usr/bin/env node
// scrollprobe — verify the TTY scrolls under real touch gestures, against the
// RUNNING app in a phone-emulated headless Chromium (coarse pointer + hasTouch,
// gestures synthesized at the compositor via CDP Input.synthesizeScrollGesture).
//
// It deep-links to '#/p/self' (the sessions rung) so NOTHING subscribes — a probe
// that lands on a session's tty claims the shared PTY size and mangles the view of
// whoever is really using that session (see memory: uiprobe binds a real session).
// The tty pane is then forced open with setView('tty') and filled locally with
// term.write, so no live claude is touched.
//
// Checks:
//   idle — a finger-pan on a quiet terminal must move viewportY off the bottom
//   held — bursts through the LIVE code path (stick = ttyAtBottom(); write;
//          if (stick) scrollToBottom()) must NOT yank the viewport back down
//
// Usage (server must be running on :8787):  cd tools && node scrollprobe.mjs
// Exit code is non-zero if a check fails, so it works in a verify flow.
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

let token = '';
try { token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim(); } catch {}

const browser = await chromium.launch({ executablePath: exec });
const ctx = await browser.newContext({ viewport: { width: 390, height: 740 }, isMobile: true, hasTouch: true });
const page = await ctx.newPage();
page.on('pageerror', e => console.error('PAGEERROR', e.message));
await page.goto(`http://127.0.0.1:${PORT}/?t=${token}#/p/self`, { waitUntil: 'load' });
await page.waitForFunction(() => typeof term !== 'undefined' && !!term.element, null, { timeout: 10000 });
await page.waitForTimeout(1200);   // let boot nav settle before checking we're session-free

const pre = await page.evaluate(() => ({ isTouch: window.matchMedia('(pointer: coarse)').matches, cid: currentCid }));
console.log('env', JSON.stringify(pre));
if (pre.cid) { console.error('ABORT: page subscribed to a real session — refusing to touch it'); await browser.close(); process.exit(2); }

await page.evaluate(async () => {
  setView('tty');                                   // no cid → shows the pane without subscribing
  await new Promise(r => setTimeout(r, 400));       // let showTTY's repaints settle
  for (let i = 1; i <= 400; i++) term.write(`line ${String(i).padStart(4, '0')}  the quick brown fox\r\n`);
  await new Promise(r => term.write('', r));        // flush the write queue
});
await page.waitForTimeout(500);

const state = () => page.evaluate(() => {
  const b = term.buffer.active;
  return { viewportY: b.viewportY, baseY: b.baseY };
});
const box = await page.evaluate(() => {
  const r = document.getElementById('term').getBoundingClientRect();
  return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
});
const cdp = await ctx.newCDPSession(page);
const pan = () => cdp.send('Input.synthesizeScrollGesture', {
  x: box.x, y: box.y, xDistance: 0, yDistance: 300,   // positive Y = scroll up (finger drags down)
  gestureSourceType: 'touch', speed: 800,
});

console.log('before', JSON.stringify(await state()));
await pan();
await page.waitForTimeout(700);
const idle = await state();
const idleOK = idle.viewportY < idle.baseY - 1;
console.log('idle  ', JSON.stringify(idle), idleOK ? 'OK: touch pan scrolled up' : 'FAIL: did not move');

// Bursts through the live stick path must not yank the viewport back down.
await page.evaluate(() => {
  window.__burst = setInterval(() => {
    const stick = ttyAtBottom();
    term.write('\x1b[2K\rtick ' + Date.now(), () => { if (stick) term.scrollToBottom(); });
  }, 100);
});
await page.waitForTimeout(1200);
const held = await state();
await page.evaluate(() => clearInterval(window.__burst));
const heldOK = held.viewportY <= idle.viewportY + 1;
console.log('held  ', JSON.stringify(held), heldOK ? 'OK: bursts did not yank' : 'FAIL: yanked back down');

await page.screenshot({ path: join(HERE, 'scrollprobe.png') });
await browser.close();
const ok = idleOK && heldOK;
console.log(ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
