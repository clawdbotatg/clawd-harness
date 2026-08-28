// pilotprobe — the 🤖 autopilot checkbox + narration row (2026-08-28). Ticking
// the box beside the state square hands the session to the harness's LLM
// supervisor; the #pilotrow above the composer narrates where the work stands.
// This guards the client half: the checkbox lives ONLY in a session view, a
// tick sends exactly one {type:"autopilot"} frame, the row appears instantly
// ("engaging…") and then mirrors pilotStatus from sessions frames, the
// optimistic tick survives a stale frame arriving before the server's echo,
// and the checkbox is a STATIC node a repaint can never rebuild mid-tap.
//
// Safe: lands on the sessions rung (#/p/self) — subscribes to nothing, claims
// no PTY size — and drives renderPilotUI()/updateTtyStat() against a FAKE
// session pushed into sessionList, with currentView() and hsend() stubbed so
// no real view opens and no frame leaves the machine.
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
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => typeof renderPilotUI === 'function').catch(() => {});
await page.waitForTimeout(600);

const r = await page.evaluate(() => {
  const CID = '00000000-probe-autopilot';
  const fake = { cid: CID, pid: 'self', title: 'probe pilot session', desc: '',
                 promptCount: 3, alive: true, busy: false,
                 autopilot: false, pilotStatus: '', pilotRounds: 0,
                 lastActive: Date.now() / 1000, promptedAt: Date.now() / 1000 };
  sessionList.push(fake);
  const sentFrames = [];
  const origHsend = hsend, origView = currentView;
  hsend = (f) => { sentFrames.push(f); return true; };
  const out = {};

  // 1) sessions rung, no open session: checkbox and row both hidden
  renderPilotUI();
  out.hiddenOffSession = pilotChk.hidden && pilotRow.hidden;

  // 2) in the session view: checkbox appears (unchecked), row still hidden
  currentCid = CID;
  currentView = () => 'tty';           // pretend, without opening a view (no resize claim)
  const nodeBefore = document.getElementById('pilotchk');
  renderPilotUI();
  out.showsInSession = !pilotChk.hidden && !pilotChk.checked && pilotRow.hidden;

  // 3) tick → exactly one autopilot frame, row appears instantly ("engaging…")
  pilotChk.checked = true;
  pilotChk.dispatchEvent(new Event('change'));
  out.tickSendsFrame = sentFrames.length === 1 &&
    sentFrames[0].type === 'autopilot' && sentFrames[0].cid === CID &&
    sentFrames[0].on === true;
  out.rowInstant = !pilotRow.hidden && pilotText.textContent.includes('engaging');

  // 4) optimistic hold: a STALE frame (autopilot still false server-side)
  //    repainting right after the tap must not untick the box
  updateTtyStat();                     // what a sessions frame runs
  out.staleFrameKeepsTick = pilotChk.checked && !pilotRow.hidden;

  // 5) the server's echo lands: row mirrors pilotStatus
  fake.autopilot = true;
  fake.pilotStatus = '▶ tests passing, wiring the UI next · round 2/20';
  pilotPendingAt = 0;                  // hold expired
  renderPilotUI();
  out.rowMirrorsStatus = pilotText.textContent.includes('wiring the UI next');

  // 6) repaint never rebuilds the checkbox (a rebuild would eat a mid-tap click)
  updateTtyStat(); updateTtyStat();
  out.staticNode = document.getElementById('pilotchk') === nodeBefore;

  // 7) untick → one on:false frame, row hides
  pilotChk.checked = false;
  pilotChk.dispatchEvent(new Event('change'));
  out.untickSendsOff = sentFrames.length === 2 && sentFrames[1].on === false;
  out.rowHides = pilotRow.hidden;

  // 8) climbing out hides both
  fake.autopilot = false; fake.pilotStatus = '';
  currentView = () => 'sessions';
  renderPilotUI();
  out.hiddenOnClimbOut = pilotChk.hidden && pilotRow.hidden;

  sessionList.splice(sessionList.findIndex(s => s.cid === CID), 1);
  currentCid = null;
  hsend = origHsend; currentView = origView;
  return out;
});
console.log('PILOT:', JSON.stringify(r));

const ok = Object.values(r).every(Boolean);
console.log(ok ? 'PASS — 🤖 checkbox session-only + static, tick/untick send one frame each, row narrates live'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
