// skillbookprobe — the 📚 fleet-skills picker (2026-08-30). Skills live in a
// private library on the relay box and sync into ~/.claude/skills on every
// machine (docs/fleet/SKILLS.md); the 📚 button (right of 🕘) fetches the
// current machine's list on open and a tap AUTO-SENDS a pointer at that
// skill's SKILL.md into the open session. This guards that chain: open sends
// skillsList, the reply renders rows, a tap outside a session is a guarded
// no-op (note row explains), and a tap inside a session sends exactly one
// 'send' frame carrying the skill path via the quick-chip path, then closes.
//
// Safe: lands on the sessions rung (#/p/self) — subscribes to nothing, claims
// no PTY size — with hsend stubbed before any tap, so nothing reaches a real
// session. Taps are REAL touch gestures (page.tap), not element.click().
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
await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => typeof showSkillbook === 'function').catch(() => {});
await page.waitForTimeout(600);

const r = {};

// stub the wire BEFORE any tap — capture every frame, deliver nothing
await page.evaluate(() => {
  window.__sent = [];
  window.__realHsend = hsend;
  hsend = (f) => { window.__sent.push(f); return true; };
  lastRx = Date.now();                          // keep deliverSend's liveness probe quiet
});

const FAKES = [
  { name: 'print-3d', description: 'send an STL to the printer', path: '/Users/x/.claude/skills/print-3d/SKILL.md' },
  { name: 'vesta', description: 'put words on the Vestaboard', path: '/Users/x/.claude/skills/vesta/SKILL.md' },
];

// 1. the button sits right of the 🕘 clock at the end of the strip
r.btnAfterClock = await page.evaluate(() =>
  document.getElementById('sentBtn').nextElementSibling === document.getElementById('skillsBtn'));

// 2. outside a session (the strip is hidden there, so no gesture to make —
//    this is the defense-in-depth path): open warns, and a row click is a
//    guarded no-op — modal stays, nothing sent
r.noopOutside = await page.evaluate((fakes) => {
  showSkillbook();
  renderSkillbook(fakes);
  const noted = !!document.querySelector('#skillbooklist .sk-note')
    && document.querySelector('#skillbooklist .sk-note').textContent.includes('open a session first');
  window.__sent = [];
  document.querySelector('#skillbooklist .sk-item').click();
  const held = !document.getElementById('skillbook').hidden && window.__sent.length === 0;
  document.getElementById('skillbook').hidden = true;
  return noted && held;
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
r.fetched = await page.evaluate(() => window.__sent.some(f => f.type === 'skillsList'));

// …the reply renders tappable rows with no warning note…
await page.evaluate((fakes) => renderSkillbook(fakes), FAKES);
r.rows = await page.evaluate(() => document.querySelectorAll('#skillbooklist .sk-item').length === 2);
r.noNote = await page.evaluate(() => !document.querySelector('#skillbooklist .sk-note'));

// …and a REAL tap on a skill sends the pointer via the quick-chip path + closes
await page.evaluate(() => { window.__sent = []; });
await page.tap('#skillbooklist .sk-item');
await page.waitForTimeout(150);
const sent = await page.evaluate(() => window.__sent);
const sendFrames = sent.filter(f => f.type === 'send');
r.sentOne = sendFrames.length === 1;
r.sentSkill = !!sendFrames[0] && sendFrames[0].via === 'quick'
  && sendFrames[0].text.includes('print-3d')
  && sendFrames[0].text.includes('/Users/x/.claude/skills/print-3d/SKILL.md');
r.closed = await page.evaluate(() => document.getElementById('skillbook').hidden);

// 5. the ✕: a REAL tap hides (sends skillsHide on:true, NO send, modal stays);
//    the hidden section expands and its ↩ unhides (on:false)
await page.tap('#skillsBtn');
await page.evaluate((fakes) => {
  window.__sent = [];
  renderSkillbook(fakes);
}, FAKES);
await page.tap('.sk-item .sk-x');
await page.waitForTimeout(120);
r.hideSent = await page.evaluate(() =>
  window.__sent.length === 1
  && window.__sent[0].type === 'skillsHide' && window.__sent[0].name === 'print-3d'
  && window.__sent[0].on === true
  && !document.getElementById('skillbook').hidden);
// server's fresh reply: print-3d now hidden → one row + a collapsed "1 hidden"
await page.evaluate((fakes) => {
  window.__sent = [];
  renderSkillbook([{ ...fakes[0], hidden: true }, fakes[1]]);
}, FAKES);
r.hiddenCollapsed = await page.evaluate(() =>
  document.querySelectorAll('#skillbooklist .sk-item').length === 1
  && document.querySelector('#skillbooklist .sk-note.tappable').textContent.includes('1 hidden'));
await page.tap('#skillbooklist .sk-note.tappable');
await page.waitForTimeout(120);
r.hiddenExpands = await page.evaluate(() =>
  document.querySelectorAll('#skillbooklist .sk-item.hiddenrow').length === 1);
await page.tap('.sk-item.hiddenrow .sk-x');
await page.waitForTimeout(120);
r.unhideSent = await page.evaluate(() =>
  window.__sent.some(f => f.type === 'skillsHide' && f.name === 'print-3d' && f.on === false)
  && !window.__sent.some(f => f.type === 'send'));
await page.evaluate(() => { document.getElementById('skillbook').hidden = true; });

// 6. a stale reply after close must not resurrect the modal
r.staleDropped = await page.evaluate(() => {
  renderSkillbook([{ name: 'late', description: '', path: '/x' }]);
  return document.getElementById('skillbook').hidden;
});

await page.evaluate(() => {   // restore what we faked (page closes right after)
  hsend = window.__realHsend; currentView = window.__realView; currentCid = null;
  document.getElementById('descrow').classList.remove('insession');
});
console.log('SKILLBOOK:', JSON.stringify(r));
const ok = r.btnAfterClock && r.modalUp && r.fetched && r.rows && r.noNote
  && r.noopOutside && r.sentOne && r.sentSkill && r.closed
  && r.hideSent && r.hiddenCollapsed && r.hiddenExpands && r.unhideSent
  && r.staleDropped;
console.log(ok ? 'PASS — 📚 opens+fetches, rows render, guarded outside a session, one tap = one pointer send, ✕ hides / ↩ restores'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
