#!/usr/bin/env node
// probe-kbd — verify (1) the key bar's ⌨ typing-mode toggle on touch, (2) the
// stale-geometry auto re-subscribe (attach at a size another device claimed →
// expect a second `subscribe` WS frame after our claim is applied).
// Sends NO keystrokes into the PTY — only DOM/WS observation. Usage:
//   node probe-kbd.mjs <pid> <cid>     (server running on :8787; pick an idle session)
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const PORT = process.env.HARNESS_PORT || '8787';
const PID = process.argv[2], CID = process.argv[3];
if (!PID || !CID) { console.error('usage: node probe-kbd.mjs <pid> <cid>'); process.exit(2); }

function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  const shells = readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of shells)
    for (const arch of ['mac-arm64', 'mac-x64']) {
      const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  return null;
}
const exec = findChromium();
const token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim();
const url = `http://127.0.0.1:${PORT}/?t=${token}#/p/${PID}/s/${CID}/tty`;
const browser = await chromium.launch({ executablePath: exec });
let failed = false;
const check = (name, ok, extra = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${extra}`);
  if (!ok) failed = true;
};

// ---- part 1: phone-sized touch page — ⌨ toggle behavior ----
{
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 700 }, hasTouch: true, isMobile: true,
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500); // let subscribe + replay + claims settle

  const coarse = await page.evaluate(() => matchMedia('(pointer: coarse)').matches);
  check('touch emulation (pointer:coarse)', coarse);

  const btn = page.locator('#kbdBtn');
  check('⌨ button visible on touch tty', await btn.isVisible());

  const before = await page.evaluate(() => ({
    kbd: document.body.classList.contains('kbdmode'),
    focused: document.activeElement?.classList?.contains('xterm-helper-textarea') || false,
    screenPE: getComputedStyle(document.querySelector('.xterm-screen')).pointerEvents,
  }));
  check('starts read-only (no kbdmode, screen click-through)',
        !before.kbd && before.screenPE === 'none', JSON.stringify(before));

  await btn.tap();
  await page.waitForTimeout(300);
  const on = await page.evaluate(() => ({
    kbd: document.body.classList.contains('kbdmode'),
    btnOn: document.getElementById('kbdBtn').classList.contains('on'),
    focused: document.activeElement?.classList?.contains('xterm-helper-textarea') || false,
    screenPE: getComputedStyle(document.querySelector('.xterm-screen')).pointerEvents,
  }));
  check('toggle ON → kbdmode + focused terminal + taps reach screen',
        on.kbd && on.btnOn && on.focused && on.screenPE === 'auto', JSON.stringify(on));

  await btn.tap();
  await page.waitForTimeout(300);
  const off = await page.evaluate(() => ({
    kbd: document.body.classList.contains('kbdmode'),
    btnOn: document.getElementById('kbdBtn').classList.contains('on'),
    focused: document.activeElement?.classList?.contains('xterm-helper-textarea') || false,
    screenPE: getComputedStyle(document.querySelector('.xterm-screen')).pointerEvents,
  }));
  check('toggle OFF → back to read-only default',
        !off.kbd && !off.btnOn && !off.focused && off.screenPE === 'none', JSON.stringify(off));

  check('no page errors (touch)', errors.length === 0, errors.join(' | '));
  // leave the phone page's size claim in place: PTY is now phone-sized
  await ctx.close();
}

// ---- part 2: desktop page attaches to the phone-sized PTY — expect auto re-subscribe ----
{
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  const sent = [];
  page.on('websocket', ws => ws.on('framesent', f => {
    try { const m = JSON.parse(f.payload); if (m.type) sent.push(m.type); } catch {}
  }));
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4500); // hello(mismatch) → claim → ttySize → +900ms re-subscribe

  const subs = sent.filter(t => t === 'subscribe').length;
  check('auto re-subscribe after geometry claim (subscribe ≥2)', subs >= 2,
        `subscribes=${subs} frames=${sent.filter(t => t !== 'input').join(',')}`);
  check('no page errors (desktop)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}

await browser.close();
process.exit(failed ? 1 : 0);
