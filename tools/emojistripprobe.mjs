#!/usr/bin/env node
// emojistripprobe — the tab strip trims each project badge against the OTHER
// TABS IN THE STRIP, not against every project fleet-wide (Austin, 09-30: the
// "mic bug" tab wore 🎤✅📱 though no other tab had a 🎤 — some project on
// another box started with 🎤, and that was enough to keep all three glyphs).
//
// Lands on the sessions rung (`#/p/self`) like tabfilterprobe, subscribes to
// nothing, then swaps in fake tabs + a fake fleet-wide code set and repaints
// the strip. Never touches a live claude.
//
// Usage (server must be running on :8787):  cd tools && node emojistripprobe.mjs

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
const page = await browser.newPage({ viewport: { width: 1400, height: 800 } });
let failed = false;
const check = (ok, m) => { console.log((ok ? 'PASS: ' : 'FAIL: ') + m); if (!ok) failed = true; };

try {
  await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
} catch (e) {
  console.error(`Could not load ${url} — is server.py running on :${PORT}?  (${e.message})`);
  await browser.close(); process.exit(2);
}
await page.waitForTimeout(1500);

const badges = await page.evaluate(() => {
  const code = { mic: '🎤✅📱', mic2: '🎤🔊', joy: '🖨️🕹️', joy2: '🖨️🎯', drop: '🩸' };
  const tabs = [
    { cid: 'fake-mic', pid: 'mic', tab: 'mic bug' },
    { cid: 'fake-joy', pid: 'joy', tab: 'joystick fix' },
    { cid: 'fake-joy-b', pid: 'joy', tab: 'joystick' },
    { cid: 'fake-joy2', pid: 'joy2', tab: 'test print' },
    { cid: 'fake-drop', pid: 'drop', tab: 'reprint' },
  ].map(t => ({ ...t, alive: true, title: t.tab }));
  // fleet-wide: another 🎤 project exists, just not in the strip
  window.allProjectEmojiCodes = () => [...Object.values(code), '🎤🎧'];
  window.railSessions = () => tabs;
  window.sessionEmojiFull = s => code[s.pid] || '';
  renderSessionBar('sessions');
  const out = {};
  for (const el of document.querySelectorAll('#sessionbar .stab')) {
    const lbl = el.querySelector('.lbl'), pe = el.querySelector('.pemoji');
    if (lbl) out[lbl.textContent] = pe ? pe.textContent : '';
  }
  out.__cardMic = shortEmoji(code.mic);   // cards/breadcrumb keep the fleet-wide form
  return out;
});

check(badges['mic bug'] === '🎤', `lone 🎤 tab shows one glyph (got ${badges['mic bug']})`);
check(badges['reprint'] === '🩸', `lone 🩸 tab shows one glyph (got ${badges['reprint']})`);
check(badges['joystick fix'] === '🖨️🕹️' && badges['test print'] === '🖨️🎯',
      `two 🖨️ projects in the strip still trim apart (got ${badges['joystick fix']} / ${badges['test print']})`);
check(badges['joystick'] === badges['joystick fix'], 'two tabs of one project wear the same badge');
check(badges.__cardMic === '🎤✅', `fleet-wide display form is untouched (got ${badges.__cardMic})`);

await browser.close();
process.exit(failed ? 1 : 0);
