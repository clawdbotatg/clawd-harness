#!/usr/bin/env node
// statprobe — assert the session-name row's layout at phone and desktop width,
// and that the state indicator (#ttystat) is a colored square whose WORDS live in
// its tooltip. Same idea as uiprobe: drive the running app from a local headless
// Chromium and read the real geometry instead of reasoning about the CSS.
//
// It never subscribes to a session and never sends anything. It fabricates the
// breadcrumb DOM that refreshBreadcrumb() would build, and calls the real
// updateTtyStat() against a stubbed one-session roster — so what it measures is
// the shipped CSS/JS, on a page that has no live session attached.
//
// Usage (server must be running on :8787):
//   cd tools && npm i        # one-time
//   node statprobe.mjs
//
// Checks:
//   * every session state paints its own class + a tooltip naming that state
//   * the indicator is a small square (no text label eating the row)
//   * phone (390px): line 1 = [box] project title · line 2 = tldr ALONE
//   * desktop (1100px): still one single line (no regression)
// Exit code is non-zero if any check fails.

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
const url = `http://127.0.0.1:${PORT}/?t=${token}`;

const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
let failed = false;
const fail = (m) => { console.error('FAIL: ' + m); failed = true; };

try {
  await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
} catch (e) {
  console.error(`Could not load ${url} — is server.py running on :${PORT}?  (${e.message})`);
  await browser.close();
  process.exit(2);
}
await page.waitForTimeout(1200);

// Fabricate the breadcrumb the way refreshBreadcrumb() does, and drive the real
// updateTtyStat() over a stubbed roster covering every state.
const states = await page.evaluate(() => {
  const descrow = document.getElementById('descrow');
  const descEl = document.getElementById('sessiondesc');
  descrow.classList.add('insession');
  descEl.textContent = '';
  const mk = (cls, txt) => { const e = document.createElement('span'); e.className = cls; e.textContent = txt; descEl.appendChild(e); };
  mk('sd-project', '🤖 clawd-harness ');
  mk('sd-title', 'compact status box');
  mk('sd-desc', 'swapping the state pill for a colored square with a hover tooltip');

  // one fake session at a time through the real sessionState()/updateTtyStat()
  const cases = [
    ['working', { alive: true, busy: true, tool: 'Bash' }],
    ['agent',   { alive: true, busy: true, tool: 'Task' }],
    ['bgwork',  { alive: true, bg: 'shell' }],
    ['waiting', { alive: true, waiting: true }],
    ['idle',    { alive: true }],
    ['dead',    { alive: false }],
  ];
  const real = window.allSessions;
  const out = [];
  for (const [want, s] of cases) {
    const sess = Object.assign({ cid: 'probe' }, s);
    window.allSessions = () => [sess];
    currentCid = 'probe';
    updateTtyStat();
    const el = document.getElementById('ttystat');
    const dot = el.querySelector('.dot');
    const cs = getComputedStyle(dot);
    out.push({ want, cls: el.className, title: el.title, aria: el.getAttribute('aria-label'),
               hidden: el.hidden, w: el.offsetWidth, h: el.offsetHeight,
               text: el.textContent.trim(), bg: cs.backgroundColor, border: cs.borderTopColor });
  }
  window.allSessions = real;
  currentCid = null;
  // leave it painted in a live-looking state for the screenshot + geometry pass
  window.allSessions = () => [{ cid: 'probe', alive: true, busy: true, tool: 'Bash' }];
  currentCid = 'probe';
  updateTtyStat();
  return out;
});

console.log('STATES', JSON.stringify(states, null, 2));
for (const s of states) {
  if (s.cls !== s.want) fail(`state ${s.want}: expected class "${s.want}", got "${s.cls}"`);
  if (s.hidden) fail(`state ${s.want}: indicator is hidden`);
  if (!(s.title || '').split('\n')[0].includes(s.want === 'agent' ? 'agent' : s.want === 'waiting' ? 'needs you' : s.want === 'bgwork' ? 'background' : s.want))
    fail(`state ${s.want}: tooltip "${(s.title || '').split('\n')[0]}" does not name the state`);
  if (!(s.aria || '').includes('session state')) fail(`state ${s.want}: missing aria-label`);
  if (s.text !== '') fail(`state ${s.want}: indicator still renders text "${s.text}" — it should be color + tooltip only`);
  if (s.w > 22 || s.h > 22) fail(`state ${s.want}: indicator is ${s.w}x${s.h}px — expected a small square`);
}
const colors = new Set(states.map(s => s.bg + '/' + s.border));
if (colors.size !== states.length) fail(`states do not all paint distinct colors: ${[...colors].join('  ')}`);

// -- geometry: phone, then desktop --------------------------------------------
const geom = async () => page.evaluate(() => {
  const box = (sel) => {
    const e = document.querySelector(sel);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), w: Math.round(r.width) };
  };
  return { row: box('#descrow'), stat: box('#ttystat'), proj: box('#sessiondesc .sd-project'),
           title: box('#sessiondesc .sd-title'), desc: box('#sessiondesc .sd-desc') };
});

await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(400);
const m = await geom();
console.log('MOBILE', JSON.stringify(m));
const near = (a, b, tol = 4) => Math.abs(a - b) <= tol;
if (!near(m.stat.top, m.proj.top, 6) || !near(m.proj.top, m.title.top, 6))
  fail('mobile: status box, project and title are not on the same line');
if (!(m.stat.left < m.proj.left)) fail('mobile: status box is not left of the project title');
if (!(m.desc.top >= m.title.bottom - 2)) fail('mobile: the tldr is not on its own line below the title');
if (!near(m.desc.left, m.row.left, 6)) fail(`mobile: the tldr is not alone on line 2 (left ${m.desc.left} vs row ${m.row.left})`);
if (!failed) console.log('PASS mobile: line 1 = [box] project title · line 2 = tldr alone.');

await page.screenshot({ path: join(HERE, 'statprobe-mobile.png') });

await page.setViewportSize({ width: 1100, height: 800 });
await page.waitForTimeout(400);
const d = await geom();
console.log('DESKTOP', JSON.stringify(d));
const tops = [d.stat.top + d.stat.w * 0, d.proj.top, d.title.top, d.desc.top];
if (!(near(d.proj.top, d.title.top) && near(d.title.top, d.desc.top)))
  fail(`desktop: the row is no longer one line (tops ${tops.join(',')})`);
if (!near(d.stat.top + Math.round(d.stat.w / 2), d.proj.top + 6, 12))
  console.log('note: status box is not vertically centred with the text (cosmetic)');
if (!(d.stat.left < d.proj.left && d.proj.left < d.title.left && d.title.left < d.desc.left))
  fail('desktop: row order is not [box] project title tldr');
if (!failed) console.log('PASS desktop: single line, order preserved.');

await page.screenshot({ path: join(HERE, 'statprobe-desktop.png') });
console.log('screenshots ->', join(HERE, 'statprobe-{mobile,desktop}.png'));
await browser.close();
process.exit(failed ? 1 : 0);
