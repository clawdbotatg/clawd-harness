// skillbookprobe — the 📚 skill LIBRARY picker (2026-08-30, rebuilt same day).
// The library is a stack of user-written skill files on the RELAY — one list
// on every machine and device, deliberately decoupled from any machine's
// installed skills. Open fetches it (skillsLib — relay socket in fleet mode,
// harness proxy in direct); a tap PASTES the skill's SKILL.md body into the
// open session; ✕ (behind a confirm) removes it from the library everywhere
// (skillsRm). This guards that chain with real touch gestures.
//
// Safe: lands on the sessions rung (#/p/self) — subscribes to nothing, claims
// no PTY size — with hsend stubbed before any tap, so nothing reaches a real
// session or the real library.
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

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
const url = `http://127.0.0.1:8787/?t=${token}#/p/self`;
const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 500, height: 850 }, hasTouch: true });
let acceptDialogs = true;                       // the ✕ confirm()
const dialogs = [];
page.on('dialog', d => { dialogs.push(d.message()); acceptDialogs ? d.accept() : d.dismiss(); });
await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => typeof showSkillbook === 'function').catch(() => {});
await page.waitForTimeout(600);

const r = {};
const FAKES = [
  { name: 'print-3d', description: 'send an STL to the printer', body: 'PROBE-BODY: how to drive the printer at 10.0.0.7' },
  { name: 'vesta', description: 'put words on the Vestaboard', body: 'vesta body' },
];

// stub the wire BEFORE any tap — capture every frame, deliver nothing
await page.evaluate(() => {
  window.__sent = [];
  window.__realHsend = hsend;
  hsend = (f) => { window.__sent.push(f); return true; };
  lastRx = Date.now();                          // keep deliverSend's liveness probe quiet
});

// 1. the button sits right of the 🕘 clock at the end of the strip
r.btnAfterClock = await page.evaluate(() =>
  document.getElementById('sentBtn').nextElementSibling === document.getElementById('skillsBtn'));

// 2. outside a session (strip hidden there — defense-in-depth path): open
//    warns, and a row click is a guarded no-op — modal stays, nothing sent
r.noopOutside = await page.evaluate((fakes) => {
  showSkillbook();
  const fetched = window.__sent.some(f => f.type === 'skillsLib');
  renderSkillbook(fakes);
  const noted = !!document.querySelector('#skillbooklist .sk-note')
    && document.querySelector('#skillbooklist .sk-note').textContent.includes('open a session first');
  window.__sent = [];
  document.querySelector('#skillbooklist .sk-item').click();
  const held = !document.getElementById('skillbook').hidden && window.__sent.length === 0;
  document.getElementById('skillbook').hidden = true;
  return fetched && noted && held;
}, FAKES);

// 3. inside a session (faked view/cid + the insession strip class — hsend
//    still stubbed) a REAL tap on 📚 opens + fetches…
await page.evaluate(() => {
  window.__realView = currentView;
  currentView = () => 'tty';
  currentCid = '00000000-probe-skillbook';
  document.getElementById('descrow').classList.add('insession');
  window.__sent = [];
});
await page.tap('#skillsBtn');
await page.waitForTimeout(150);
r.modalUp = await page.evaluate(() => !document.getElementById('skillbook').hidden);
r.fetched = await page.evaluate(() => window.__sent.some(f => f.type === 'skillsLib'));

// …the reply renders rows (name + description) with no warning note…
await page.evaluate((fakes) => renderSkillbook(fakes), FAKES);
r.rows = await page.evaluate(() => document.querySelectorAll('#skillbooklist .sk-item').length === 2);
r.noNote = await page.evaluate(() => !document.querySelector('#skillbooklist .sk-note'));

// …and a REAL tap on a skill pastes its BODY via the quick-chip path + closes
await page.evaluate(() => { window.__sent = []; });
await page.tap('#skillbooklist .sk-item');
await page.waitForTimeout(150);
const sent = await page.evaluate(() => window.__sent);
const sendFrames = sent.filter(f => f.type === 'send');
r.sentOne = sendFrames.length === 1;
r.sentBody = !!sendFrames[0] && sendFrames[0].via === 'quick'
  && sendFrames[0].text === FAKES[0].body;
r.closed = await page.evaluate(() => document.getElementById('skillbook').hidden);

// 4. the ✕: confirm-gated remove — dismiss keeps it, accept sends skillsRm
//    (and never a 'send'), modal stays open awaiting the fresh-list reply
await page.tap('#skillsBtn');
await page.evaluate((fakes) => { window.__sent = []; renderSkillbook(fakes); }, FAKES);
acceptDialogs = false;
await page.tap('.sk-item .sk-x');
await page.waitForTimeout(150);
r.rmNeedsConfirm = dialogs.length === 1 && dialogs[0].includes('print-3d');
r.rmDismissed = await page.evaluate(() => window.__sent.length === 0);
acceptDialogs = true;
await page.tap('.sk-item .sk-x');
await page.waitForTimeout(150);
r.rmSent = await page.evaluate(() =>
  window.__sent.length === 1
  && window.__sent[0].type === 'skillsRm' && window.__sent[0].name === 'print-3d'
  && !document.getElementById('skillbook').hidden);

// 5. reply states: an error note renders; a stale reply after close is dropped
r.errShown = await page.evaluate(() => {
  renderSkillbook([], 'relay unreachable: probe');
  const n = document.querySelector('#skillbooklist .sk-note');
  return !!n && n.textContent.includes('relay unreachable');
});
r.staleDropped = await page.evaluate(() => {
  document.getElementById('skillbook').hidden = true;
  renderSkillbook([{ name: 'late', description: '', body: 'x' }]);
  return document.getElementById('skillbook').hidden;
});

await page.evaluate(() => {   // restore what we faked (page closes right after)
  hsend = window.__realHsend; currentView = window.__realView; currentCid = null;
  document.getElementById('descrow').classList.remove('insession');
});
console.log('SKILLBOOK:', JSON.stringify(r));
const ok = r.btnAfterClock && r.noopOutside && r.modalUp && r.fetched && r.rows
  && r.noNote && r.sentOne && r.sentBody && r.closed
  && r.rmNeedsConfirm && r.rmDismissed && r.rmSent && r.errShown && r.staleDropped;
console.log(ok ? 'PASS — 📚 fetches the library, one tap pastes the body, ✕ confirm-removes, errors render'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
