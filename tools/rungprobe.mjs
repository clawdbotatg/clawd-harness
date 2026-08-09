// rungprobe — assert the projects rung survives a repaint.
//
//   cd tools && node rungprobe.mjs        (server must be running on :8787)
//
// The rung repaints on every `projects` frame, and the server sends one whenever
// session state moves — during a live turn that was a couple per tool call, times
// the machines in fleet mode. The old renderer answered each with
// `projectsEl.innerHTML = ''`, which cost three things at once, several times a
// second, and read to the user as "the page is possessed":
//   • #projectlist IS the scroll container, so emptying it collapsed scrollHeight
//     and the browser clamped scrollTop to 0 — the list jerked back to the top;
//   • the tail autofocus re-grabbed the filter box on every repaint (not just on
//     arrival), stealing focus and scrolling it into view;
//   • the re-created <input> was refilled from the projectFilter mirror, so any
//     text the `input` event hadn't mirrored yet was dropped — on touch,
//     composition/dictation lose whole words.
// So the checks below are the three symptoms, not the implementation: scroll
// holds, focus is untouched, in-flight text survives. `renderProjects(projectList)`
// is exactly what a `projects` WS frame does, so no session is ever touched.
//
// Exit code is non-zero if a check fails, so it works in a verify flow.
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = dirname(HERE);
const PORT = process.env.HARNESS_PORT || '8787';
function findChromium(){const c=join(process.env.HOME,'Library/Caches/ms-playwright');
 for(const d of readdirSync(c).filter(d=>d.startsWith('chromium_headless_shell-')).sort().reverse())
  for(const a of ['mac-arm64','mac-x64']){const b=join(c,d,`chrome-headless-shell-${a}`,'chrome-headless-shell');if(existsSync(b))return b;}}
let token = '';
try { token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim(); } catch {}

const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 900, height: 600 } });
const errs = [];
page.on('pageerror', e => errs.push(e.message));
await page.goto(`http://127.0.0.1:${PORT}/?t=${token}#/`);
await page.waitForTimeout(2500);

let bad = 0;
const ok = (n, c, extra = '') => { console.log((c ? '  ✓ ' : '  ✗ FAIL ') + n + (extra ? `  ${extra}` : '')); if (!c) bad++; };

// Arrival — the ONE thing still allowed to take the filter box's focus.
const arrive = await page.evaluate(() => {
  document.activeElement && document.activeElement.blur();
  setView('projects');
  return { focused: document.activeElement === document.querySelector('#addProject input'),
           cards: document.querySelectorAll('#projcards .scard').length };
});
ok('landing on the rung focuses the filter box', arrive.focused);
ok('the rung renders its cards', arrive.cards > 0, `cards=${arrive.cards}`);

// A repaint with nobody touching anything: scroll and focus must not move.
const rp = await page.evaluate(() => {
  const el = document.getElementById('projectlist');
  el.scrollTop = 99999;                        // read something far down the list
  document.activeElement && document.activeElement.blur();
  const before = el.scrollTop, firstCard = el.querySelector('.scard');
  renderProjects(projectList);                 // ← exactly what a `projects` frame does
  return { before, after: el.scrollTop, active: document.activeElement.tagName,
           reused: firstCard === el.querySelector('.scard'),
           cards: el.querySelectorAll('.scard').length };
});
ok('scroll position survives a repaint', rp.before > 0 && rp.after === rp.before, `${rp.before} → ${rp.after}`);
ok('a repaint never steals focus', rp.active === 'BODY', `activeElement=${rp.active}`);
ok('card nodes are reused, not rebuilt', rp.reused);
ok('the card list is unchanged', rp.cards === arrive.cards, `cards=${rp.cards}`);

// A repaint landing mid-word. `value` is set without dispatching `input` on
// purpose: that is the state a composing/dictating keyboard leaves the box in.
const typed = await page.evaluate(() => {
  const inp = document.querySelector('#addProject input');
  inp.focus();
  inp.value = 'ab';
  renderProjects(projectList);
  const now = document.querySelector('#addProject input');
  const r = { value: now.value, same: now === inp, focused: document.activeElement === now };
  now.value = ''; now.dispatchEvent(new Event('input'));
  return r;
});
ok('in-flight text survives a repaint', typed.value === 'ab', `value=${JSON.stringify(typed.value)}`);
ok('the input node is never replaced', typed.same);
ok('the input keeps focus', typed.focused);

// The filter still filters, and code-driven writes still reach the box.
const filt = await page.evaluate(() => {
  const inp = document.querySelector('#addProject input');
  inp.value = 'zzzznope'; inp.dispatchEvent(new Event('input'));
  const hidden = [...document.querySelectorAll('#projcards .scard')].every(x => x.style.display === 'none');
  inp.value = ''; inp.dispatchEvent(new Event('input'));
  const shown = [...document.querySelectorAll('#projcards .scard')].every(x => x.style.display === '');
  setProjectFilter('xyz'); const set = inp.value;
  setProjectFilter('');
  return { hidden, shown, set, cleared: inp.value };
});
ok('typing filters the list', filt.hidden);
ok('clearing the filter shows everything again', filt.shown);
ok('setProjectFilter() drives the box', filt.set === 'xyz' && filt.cleared === '');
ok('no uncaught page errors', errs.length === 0, errs.join(' | '));

await page.screenshot({ path: join(HERE, 'rungprobe.png') });
await browser.close();
console.log(bad ? `\nFAILED (${bad})` : '\nPASS — the projects rung holds still while it repaints.');
process.exit(bad ? 1 : 0);
