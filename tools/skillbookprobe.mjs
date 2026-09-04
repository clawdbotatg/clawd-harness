// skillbookprobe — the 📚 skill LIBRARY picker (2026-08-30; tap→attach 2026-09-04).
// The library is a stack of user-written skill files on the RELAY — one list
// on every machine and device, deliberately decoupled from any machine's
// installed skills. Open fetches it (skillsLib — relay socket in fleet mode,
// harness proxy in direct); a tap ATTACHES the skill — its SKILL.md body is
// uploaded like a dropped .md file and a 📚 chip sits above the composer,
// nothing is sent — and Enter sends your text plus one instruction line per
// skill pointing at the uploaded path; ✕ on the row (behind a confirm) removes
// it from the library everywhere (skillsRm). This guards that chain with real
// touch gestures and real keystrokes.
//
// Safe: lands on the sessions rung (#/p/self) — subscribes to nothing, claims
// no PTY size — with hsend AND the /upload fetch stubbed before any tap, so
// nothing reaches a real session, the real library, or the uploads dir.
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

// stub the wire BEFORE any tap — capture every frame, deliver nothing; the
// /upload POST is answered locally too (a fake path back, or a 413 on demand)
const UP_PATH = '/probe/uploads/paste-0000-print-3d-SKILL.md';
await page.evaluate((UP_PATH) => {
  window.__sent = [];
  window.__realHsend = hsend;
  hsend = (f) => { window.__sent.push(f); return true; };
  lastRx = Date.now();                          // keep deliverSend's liveness probe quiet
  window.__uploads = []; window.__upFail = false;
  window.__realFetch = window.fetch;
  window.fetch = async (url, init) => {
    if (!String(url).startsWith('/upload')) return window.__realFetch(url, init);
    window.__uploads.push({ hdr: init.headers['Content-Type'], body: await init.body.text() });
    if (window.__upFail) return new Response('too big', { status: 413 });
    return new Response(JSON.stringify({ path: UP_PATH, name: 'paste-0000-print-3d-SKILL.md' }),
                        { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
}, UP_PATH);

// 1. the button sits right of the 🕘 clock at the end of the strip
r.btnAfterClock = await page.evaluate(() =>
  document.getElementById('sentBtn').nextElementSibling === document.getElementById('skillsBtn'));

// 2. outside any project (strip hidden there — defense-in-depth path): open
//    warns, and a row click is a guarded no-op — modal stays, no chip, no send
r.noopOutside = await page.evaluate((fakes) => {
  window.__realView = currentView;
  currentView = () => 'projects';
  showSkillbook();
  const fetched = window.__sent.some(f => f.type === 'skillsLib');
  renderSkillbook(fakes);
  const noted = !!document.querySelector('#skillbooklist .sk-note')
    && document.querySelector('#skillbooklist .sk-note').textContent.includes('open a project or session first');
  window.__sent = [];
  document.querySelector('#skillbooklist .sk-item').click();
  const held = !document.getElementById('skillbook').hidden && window.__sent.length === 0
    && window.__uploads.length === 0 && attachments.length === 0;
  document.getElementById('skillbook').hidden = true;
  return fetched && noted && held;
}, FAKES);

// 3. inside a session (faked view/cid + the insession strip class — hsend
//    still stubbed) a REAL tap on 📚 opens + fetches…
await page.evaluate(() => {
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

// …and a REAL tap on a skill ATTACHES it: the body goes up the /upload
// pipeline as a markdown file, a 📚 chip appears over the composer, the modal
// closes, the box has focus — and NOTHING is sent
await page.evaluate(() => { window.__sent = []; });
await page.tap('#skillbooklist .sk-item');
await page.waitForTimeout(250);
r.closed = await page.evaluate(() => document.getElementById('skillbook').hidden);
r.uploaded = await page.evaluate((fakes) => window.__uploads.length === 1
  && window.__uploads[0].hdr.startsWith('text/markdown') && window.__uploads[0].hdr.includes('print-3d-SKILL.md')
  && window.__uploads[0].body === fakes[0].body, FAKES);
r.chip = await page.evaluate(() => {
  const c = document.querySelector('#attachments .chip.skill');
  return !!c && c.textContent.includes('📚') && c.textContent.includes('print-3d') && !c.classList.contains('err')
    && attachments.length === 1 && attachments[0].path === '/probe/uploads/paste-0000-print-3d-SKILL.md';
});
r.notSent = await page.evaluate(() => !window.__sent.some(f => f.type === 'send'));
r.boxFocused = await page.evaluate(() => document.activeElement === box);

// tapping the same skill again is a no-op (one chip per skill)…
await page.tap('#skillsBtn');
await page.evaluate((fakes) => renderSkillbook(fakes), FAKES);
await page.tap('#skillbooklist .sk-item');
await page.waitForTimeout(250);
r.dedup = await page.evaluate(() => attachments.length === 1 && window.__uploads.length === 1);

// …then REAL typing + 📤 sends ONE message: the text, newline, the
// instruction line pointing at the uploaded path — and the chip is gone
await page.evaluate(() => { window.__sent = []; box.value = ''; });
await page.focus('#box');
await page.keyboard.type('print the bracket');
await page.tap('#send');                         // touch: Enter is a newline, 📤 sends
await page.waitForTimeout(200);
const sent = await page.evaluate(() => window.__sent);
const sendFrames = sent.filter(f => f.type === 'send');
r.sentOne = sendFrames.length === 1;
r.sentComposed = !!sendFrames[0] && sendFrames[0].text ===
  `print the bracket\nUse the "print-3d" skill: read ${UP_PATH} and follow it. If it needs details I haven't given, ask me.`;
r.chipGone = await page.evaluate(() => attachments.length === 0 && !document.querySelector('#attachments .chip'));

// the chip's ✕ drops it without sending; a failed upload stays as a red chip
await page.tap('#skillsBtn');
await page.evaluate((fakes) => { window.__sent = []; renderSkillbook(fakes); }, FAKES);
await page.tap('#skillbooklist .sk-item');
await page.waitForTimeout(250);
await page.tap('#attachments .chip.skill button');
r.chipX = await page.evaluate(() => attachments.length === 0 && !window.__sent.some(f => f.type === 'send'));
await page.evaluate(() => { window.__upFail = true; });
await page.tap('#skillsBtn');
await page.evaluate((fakes) => renderSkillbook(fakes), FAKES);
await page.tap('#skillbooklist .sk-item');
await page.waitForTimeout(250);
r.failChip = await page.evaluate(() => {
  const c = document.querySelector('#attachments .chip.skill.err');
  return !!c && c.textContent.includes('413') && attachments.length === 1 && !attachments[0].path;
});
await page.evaluate(() => { window.__upFail = false; attachments = []; renderChips(); });

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
  hsend = window.__realHsend; window.fetch = window.__realFetch; currentView = window.__realView; currentCid = null;
  document.getElementById('descrow').classList.remove('insession');
});
console.log('SKILLBOOK:', JSON.stringify(r));
const ok = r.btnAfterClock && r.noopOutside && r.modalUp && r.fetched && r.rows
  && r.noNote && r.closed && r.uploaded && r.chip && r.notSent && r.boxFocused && r.dedup
  && r.sentOne && r.sentComposed && r.chipGone && r.chipX && r.failChip
  && r.rmNeedsConfirm && r.rmDismissed && r.rmSent && r.errShown && r.staleDropped;
console.log(ok ? 'PASS — 📚 fetches the library, a tap attaches a chip (nothing sent), Enter composes text + pointer line, ✕ confirm-removes, errors render'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
