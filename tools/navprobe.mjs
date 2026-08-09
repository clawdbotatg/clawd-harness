#!/usr/bin/env node
// navprobe — what does the 💬 sessions button actually DO from each starting rung?
// Reproduces the reported bug ("clicking the chat bubble doesn't show me the session
// tabs") by clicking the real button in the real app and reporting the resulting
// view + whether the #sessionbar tab strip is on screen.
//
//   node navprobe.mjs
//
// Read-only: it navigates rungs and clicks header buttons. It never subscribes to
// a session, types, or sends.

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
if (!exec) { console.error('no cached playwright chromium'); process.exit(2); }
let token = '';
try { token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim(); } catch {}

const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });

const state = () => page.evaluate(() => {
  const bar = document.getElementById('sessionbar');
  return {
    hash: location.hash,
    view: (typeof currentView === 'function') ? currentView() : '?',
    currentPid: (typeof currentPid !== 'undefined') ? currentPid : '?',
    sessionTabs: bar.hidden ? 0 : bar.querySelectorAll('.stab').length,
    barHidden: bar.hidden,
    menuBtnOn: document.getElementById('menuBtn').classList.contains('on'),
  };
});

// COLD START: no remembered project. This is the state the bug report describes —
// open the app fresh, press 💬 before ever picking a project.
await page.goto(`http://127.0.0.1:${PORT}/?t=${token}`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1200);
await page.evaluate(() => { try { localStorage.removeItem('cc_view'); localStorage.removeItem('cc_pid'); } catch {} })
          .catch(() => {});
await page.goto(`http://127.0.0.1:${PORT}/?t=${token}#/`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2500);   // let the WS deliver projects + sessions

console.log('COLD start:            ', JSON.stringify(await state()));
await page.click('#menuBtn');
await page.waitForTimeout(700);
console.log('  → after 💬:          ', JSON.stringify(await state()), '  <-- expected view:sessions');

// NO PROJECT SELECTED — the reported failure. Direct mode always restores a pid,
// so force the state the fleet UI boots into and press 💬.
await page.evaluate(() => { currentPid = null; currentProjectKey = null; renderSessions(); });
await page.click('#projectsBtn');
await page.waitForTimeout(500);
await page.evaluate(() => { currentPid = null; currentProjectKey = null; });
await page.click('#menuBtn');
await page.waitForTimeout(700);
const noProj = await page.evaluate(() => {
  const bar = document.getElementById('sessionbar');
  return {
    view: currentView(),
    heading: (document.querySelector('#sessions .menutitle') || {}).textContent,
    cards: document.querySelectorAll('#sessions .scard').length,
    sessionTabs: bar.hidden ? 0 : bar.querySelectorAll('.stab').length,
    newBtn: (document.getElementById('newSession') || {}).textContent,
  };
});
console.log('NO project + 💬:       ', JSON.stringify(noProj));

let bad = 0;
if (noProj.view !== 'sessions') { console.log('FAIL: 💬 did not land on the sessions rung'); bad = 1; }
if (!noProj.cards)              { console.log('FAIL: sessions rung listed nothing'); bad = 1; }
if (!noProj.sessionTabs)        { console.log('FAIL: no session tabs visible'); bad = 1; }
console.log(bad ? 'FAILED' : 'PASS — 💬 shows sessions with no project selected');

// ⚙️ settings page: opens, lists the notifications row, closes on Esc.
await page.click('#settingsBtn');
await page.waitForTimeout(500);
const setOpen = await page.evaluate(() => ({
  open: !document.getElementById('settingsmodal').hidden,
  // not every row is a toggle any more — "where new projects land" is a <select>
  // (see renderSettings), so read whichever control the row actually carries.
  rows: [...document.querySelectorAll('#settingsbody .setrow')].map(r => {
    const ctl = r.querySelector('.settoggle') || r.querySelector('select');
    return { t: r.querySelector('.set-t').textContent,
             ctl: ctl ? ctl.textContent : '', disabled: ctl ? ctl.disabled : false };
  }),
}));
console.log('⚙️ settings:           ', JSON.stringify(setOpen));

// Direct mode reports push "unavailable" (fleet-only), so the real on/off control
// never renders here. Build both states directly to check the control itself —
// which branch gets picked is separately covered by the reads above.
const toggleLook = await page.evaluate(() => {
  const out = [];
  for (const on of [true, false]) {
    const r = settingRow('notifications', 'x', { label: on ? 'on' : 'off', on, onToggle: () => {} });
    document.getElementById('settingsbody').appendChild(r);
    const b = r.querySelector('.settoggle');
    out.push({ label: b.textContent, on: b.classList.contains('on'), disabled: b.disabled });
  }
  return out;
});
console.log('  toggle states:       ', JSON.stringify(toggleLook));
if (toggleLook[0].label !== 'on' || !toggleLook[0].on || toggleLook[0].disabled) {
  console.log('FAIL: "on" toggle did not render as an enabled, lit control'); bad = 1;
}
if (toggleLook[1].label !== 'off' || toggleLook[1].on) {
  console.log('FAIL: "off" toggle rendered wrong'); bad = 1;
}
await page.screenshot({ path: join(HERE, 'settings.png') });
await page.keyboard.press('Escape');
await page.waitForTimeout(300);
const setClosed = await page.evaluate(() => document.getElementById('settingsmodal').hidden);
if (!setOpen.open)                       { console.log('FAIL: ⚙️ did not open'); bad = 1; }
if (setOpen.rows[0]?.t !== 'notifications') { console.log('FAIL: no notifications row'); bad = 1; }
if (!setClosed)                          { console.log('FAIL: Esc did not close settings'); bad = 1; }
console.log(bad ? 'FAILED' : 'PASS — settings page opens, lists notifications, closes on Esc');

await page.screenshot({ path: join(HERE, 'navprobe.png') });
console.log('screenshot -> tools/navprobe.png');
await browser.close();
