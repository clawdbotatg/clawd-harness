// voiceprobe — drive the 🎙 voice PM in a real browser with everything below
// the button faked, and assert the whole client loop.
//
//   cd tools && node voiceprobe.mjs
//
// What it pins down (each is a real trap from the gpt-voice reference or this
// repo's own probes):
//   1. The 🎙 talk button exists in the PM bar and a click reaches LIVE.
//   2. A tool-call event executes against the right /pm endpoint (whats_waiting
//      → /api/tool name:"sweep") and sends BOTH data-channel events back —
//      function_call_output then response.create; forget the second and the
//      model silently never speaks the result (INTEGRATION.md trap #2).
//   3. Transcript events land as bubbles in the PM feed.
//   4. The session survives leaving the PM view (the fixed HUD is the handle).
//   5. Hang-up tears down: mic stopped, channel closed, HUD gone.
//
// Needs no live controller, no OpenAI key, no mic: /pm/* and api.openai.com are
// intercepted, and getUserMedia/RTCPeerConnection are stubbed. It only ever
// touches the PM surface, never a real session.
//
// Exit code is non-zero if a check fails — so it works in a verify flow.

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

const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 }, serviceWorkers: 'block' });

// -- stub the media/WebRTC layer BEFORE the app loads -------------------------
// The fake data channel is reachable at window.__vdc so the probe can inject
// server→client events and read what the client sent back.
await page.addInitScript(() => {
  window.__micStopped = 0;
  window.__micConstraints = null;
  window.__pcCount = 0;
  navigator.mediaDevices = navigator.mediaDevices || {};
  navigator.mediaDevices.getUserMedia = async (c) => {
    window.__micConstraints = c;
    window.__micTrack = { enabled: true, stop: () => { window.__micStopped++; } };
    return { getTracks: () => [window.__micTrack] };
  };
  window.RTCPeerConnection = class {
    constructor() { this.ontrack = null; window.__pcCount++; }
    addTrack() {}
    createDataChannel(name) {
      const dc = {
        label: name, readyState: 'open', sent: [], onmessage: null, onclose: null,
        send(s) { this.sent.push(JSON.parse(s)); },
        close() { if (this.readyState === 'closed') return; this.readyState = 'closed'; if (this.onclose) this.onclose(); },
      };
      window.__vdc = dc;
      return dc;
    }
    async createOffer() { return { type: 'offer', sdp: 'v=0 fake-offer' }; }
    async setLocalDescription() {}
    async setRemoteDescription(d) { window.__answerSdp = d && d.sdp; }
    close() { window.__pcClosed = true; }
  };
});

// -- fake controller + fake OpenAI -------------------------------------------
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
const toolCalls = [];   // every POST /pm/api/tool body
let tokenMints = 0;     // every POST /pm/api/voice/token

await page.route('**/pm/**', async route => {
  const url = new URL(route.request().url());
  const p = url.pathname.replace(/^\/pm/, '');
  const post = () => { try { return JSON.parse(route.request().postData() || '{}'); } catch { return {}; } };
  if (p === '/api/state') return json(route, { autonomy: 'auto', backend: 'claude', model: '', models: [],
                                               machines: [], attention_count: 0,
                                               harness: { base: '', token: '', port: 8787 } });
  if (p === '/api/threads') return json(route, { threads: [{ id: 't1', title: 'alpha', archived: false, count: 0, msgs: 0, current: true }], current: 't1', archived_count: 0 });
  if (p === '/api/thread/messages') return json(route, { messages: [] });
  if (p === '/api/voice/token') {
    // Slow mint on purpose: it opens the connecting window in which the 08-16
    // chaos lived — extra taps during it must NOT mint extra sessions.
    tokenMints++;
    await new Promise(r => setTimeout(r, 400));
    return json(route, {
      value: 'ek_test_secret', expires_at: 9999999999,
      exec: { whats_waiting: { kind: 'verb', name: 'sweep' },
              read_lore: { kind: 'lore' }, ask_pm: { kind: 'chat' } },
      model: 'gpt-realtime', voice: 'marin' });
  }
  if (p === '/api/tool') { const b = post(); toolCalls.push(b); return json(route, { tool: b.name, args: b.args, result: { attention: [], note: 'all quiet' } }); }
  if (p === '/api/chat') return json(route, { reply: 'pm did the thing', trace: [] });
  if (p.startsWith('/api/voice/lore')) return json(route, { page: 'soul', text: 'crab lore' });
  return json(route, {});
});
await page.route('**/api.openai.com/**', route =>
  route.fulfill({ status: 200, contentType: 'application/sdp', body: 'v=0 fake-answer' }));

const errors = [];
page.on('pageerror', e => errors.push(String(e)));

let failed = false;
const check = (name, ok, detail) => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${ok || !detail ? '' : ' — ' + detail}`);
  if (!ok) failed = true;
};

try {
  await page.goto(`http://127.0.0.1:${PORT}/?t=${token}#/pm`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForSelector('#pmfeed', { state: 'visible', timeout: 8000 });

  // 1 — the button; a NERVOUS TRIPLE-TAP must still yield exactly one session.
  // (2026-08-16 in prod: the mint takes seconds, extra taps each minted another
  // full realtime session, and the sessions answered each other's speaker
  // output — "three voices, stacked chaos". The claim is now synchronous.)
  const talk = page.locator('#sessionbar .pmctl', { hasText: '🎙' }).first();
  check('🎙 talk button in the PM bar', await talk.count() === 1);
  await talk.click();
  await page.waitForTimeout(80);      // land the next taps INSIDE the connecting window
  await talk.click();
  await talk.click();
  await page.waitForFunction(() =>
    document.querySelector('#voicehud .vstate')?.textContent === 'LIVE', null, { timeout: 5000 });
  check('triple-tap → HUD reaches LIVE', true);
  await page.waitForTimeout(600);     // any ghost session would finish minting by now
  check('…and exactly ONE token minted', tokenMints === 1, `mints=${tokenMints}`);
  check('…and exactly ONE peer connection', await page.evaluate(() => window.__pcCount) === 1);
  check('mic asked for echo cancellation',
        await page.evaluate(() => window.__micConstraints?.audio?.echoCancellation === true));
  check('SDP answer applied', await page.evaluate(() => window.__answerSdp === 'v=0 fake-answer'));
  check('button flips to live', await page.locator('#sessionbar .pmctl.vlive').count() === 1);

  // helper: inject a server→client data-channel event
  const inject = ev => page.evaluate(e => window.__vdc.onmessage({ data: JSON.stringify(e) }), ev);

  // 2 — tool call round-trip
  await inject({ type: 'response.function_call_arguments.done', name: 'whats_waiting', arguments: '{}', call_id: 'c1' });
  await page.waitForFunction(() => (window.__vdc.sent || []).length >= 2, null, { timeout: 5000 });
  check('whats_waiting hit /pm/api/tool as sweep',
        toolCalls.length === 1 && toolCalls[0].name === 'sweep', JSON.stringify(toolCalls));
  const sent = await page.evaluate(() => window.__vdc.sent);
  const outIdx = sent.findIndex(s => s.type === 'conversation.item.create' && s.item?.type === 'function_call_output' && s.item.call_id === 'c1');
  const speakIdx = sent.findIndex((s, i) => i > outIdx && s.type === 'response.create');
  check('function_call_output sent with call_id', outIdx !== -1, JSON.stringify(sent));
  check('response.create follows (the "make it speak" event)', speakIdx > outIdx, JSON.stringify(sent));
  check('tool result carries real data', outIdx !== -1 && sent[outIdx].item.output.includes('all quiet'));

  // 2.5 — duplex policy. Desktop DEFAULTS to full-duplex (barge-in, browser
  // AEC trusted); flipping 🎧 off gives speaker-safe half-duplex: mic dead
  // while its audio plays (the fix for "it hears itself and loops"), finger
  // interrupt instead of voice.
  await inject({ type: 'output_audio_buffer.started' });
  check('desktop default: mic stays OPEN while it talks (full-duplex)',
        await page.evaluate(() => window.__micTrack.enabled === true));
  await inject({ type: 'output_audio_buffer.stopped' });
  await page.click('#voicehud .vhp');                       // explicit 🎧 OFF → speaker-safe
  await inject({ type: 'output_audio_buffer.started' });
  check('🎧 off: mic hard-muted while assistant audio plays',
        await page.evaluate(() => window.__micTrack.enabled === false));
  await inject({ type: 'output_audio_buffer.stopped' });
  await page.waitForFunction(() => window.__micTrack.enabled === true, null, { timeout: 2000 });
  check('mic back after playback (+reverb tail)', true);
  // tap-to-interrupt: with the mic dead, the finger does what the voice can't
  await inject({ type: 'output_audio_buffer.started' });
  const sentBefore = await page.evaluate(() => window.__vdc.sent.length);
  await page.click('#voicehud .vstate');
  const tail = await page.evaluate(n => window.__vdc.sent.slice(n), sentBefore);
  check('tap the state word → response.cancel + buffer clear',
        tail.some(s => s.type === 'response.cancel') && tail.some(s => s.type === 'output_audio_buffer.clear'),
        JSON.stringify(tail));
  check('…and the mic reopens instantly', await page.evaluate(() => window.__micTrack.enabled === true));

  // 3 — transcripts land as feed bubbles
  await inject({ type: 'conversation.item.input_audio_transcription.completed', transcript: 'what needs me today' });
  await inject({ type: 'response.output_audio_transcript.done', transcript: 'all quiet on the fleet' });
  const feed = await page.locator('#pmfeed').innerText();
  check('user transcript bubble', feed.includes('what needs me today'), feed.slice(0, 300));
  check('assistant transcript bubble', feed.includes('all quiet on the fleet'));
  check('tool call visible in feed', feed.includes('whats_waiting'));

  // 4 — session survives leaving the PM view
  await page.evaluate(() => navTo('projects'));
  await page.waitForTimeout(300);
  check('HUD survives leaving the PM view',
        await page.evaluate(() => !document.getElementById('voicehud').hidden &&
                                  window.__vdc.readyState === 'open'));

  // 5 — hang up from the HUD tears everything down
  await page.click('#voicehud .vx');
  await page.waitForTimeout(200);
  check('hang-up: HUD hidden', await page.evaluate(() => document.getElementById('voicehud').hidden));
  check('hang-up: mic stopped', await page.evaluate(() => window.__micStopped >= 1));
  check('hang-up: data channel + pc closed',
        await page.evaluate(() => window.__vdc.readyState === 'closed' && window.__pcClosed === true));

  check('no page errors', errors.length === 0, errors.join(' | '));
} catch (e) {
  check('probe completed', false, String(e));
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
