#!/usr/bin/env node
// probe-geom — REPRODUCE the mangled-attach bug visually: a phone-sized client
// claims the PTY, then a desktop client attaches. Screenshots the desktop view
// mid-replay (expected: phone-geometry wreckage) and after the auto re-subscribe
// (expected: clean). No keystrokes are sent. Usage: node probe-geom.mjs <pid> <cid>
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const PORT = process.env.HARNESS_PORT || '8787';
const PID = process.argv[2], CID = process.argv[3];
const OUT = process.argv[4] || HERE;
if (!PID || !CID) { console.error('usage: node probe-geom.mjs <pid> <cid> [outdir]'); process.exit(2); }

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
const token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim();
const url = `http://127.0.0.1:${PORT}/?t=${token}#/p/${PID}/s/${CID}/tty`;
const browser = await chromium.launch({ executablePath: findChromium() });

// 1. phone client attaches + claims the PTY size, stays a beat so the TUI repaints
//    at phone geometry into the ring, then leaves.
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 700 }, hasTouch: true, isMobile: true });
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3500);
  console.log('phone client attached, claimed, leaving — PTY is now phone-sized');
  await ctx.close();
}

// 2. desktop client attaches to the phone-sized PTY.
{
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await ctx.newPage();
  const sent = [];
  page.on('websocket', ws => ws.on('framesent', f => {
    try { const m = JSON.parse(f.payload); if (m.type === 'subscribe') sent.push(Date.now()); } catch {}
  }));
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);            // replay landed; auto-heal hasn't fired yet
  await page.screenshot({ path: join(OUT, 'geom-before.png') });
  console.log('before shot @800ms, subscribes so far:', sent.length);
  await page.waitForTimeout(3700);           // ttySize + 900ms re-subscribe + repaint
  await page.screenshot({ path: join(OUT, 'geom-after.png') });
  console.log('after shot @4.5s, subscribes total:', sent.length);
  await ctx.close();
}
await browser.close();
