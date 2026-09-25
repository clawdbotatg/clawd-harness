// sendackprobe — ✓ send receipts (2026-09-25). Austin sent a photo to a codex
// session on omen mid-task: codex holds a message until its current step ends
// (a minute+) and shows nothing, and a reload put the text back in the box as
// if unsent — so he re-sent the photo twice. Contract pinned:
//   1. every send frame carries an id; its queued box reads "⏳ sending";
//   2. the machine's sendAck turns it into "✓ on <machine> · waiting";
//   3. a RELOAD keeps that ✓ box and leaves the composer EMPTY (was: the text
//      came back into the box, photo-less, looking unsent);
//   4. the prompt landing in the transcript clears the box + the receipt;
//   5. no receipt in SEND_ACK_MS → "⚠ not delivered" + resend; resend sends a
//      fresh id; a late receipt for the first try doesn't mark the new box;
//   6. an error receipt (unknown session) → "⚠ not delivered" at once;
//   6b. a send that lands while you view another session leaves no ghost box;
//   7. before any receipt, a reload still puts the text back in the box.
// Fleet mode + stubbed relay WebSocket + stubbed hsend (uploadwaitprobe pattern).
//   cd tools && node sendackprobe.mjs
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = dirname(HERE);
function findChromium(){const c=join(process.env.HOME,'Library/Caches/ms-playwright');
 for(const d of readdirSync(c).filter(d=>d.startsWith('chromium_headless_shell-')).sort().reverse())
  for(const a of ['mac-arm64','mac-x64']){const b=join(c,d,`chrome-headless-shell-${a}`,'chrome-headless-shell');if(existsSync(b))return b;}}
const raw = readFileSync(join(ROOT,'index.html'),'utf8');
const fleetHtml = raw.replace('<head>','<head><script>window.__FLEET__=true;</script>');
const browser = await chromium.launch({ executablePath: findChromium() });
let failed=false; const check=(n,ok,d)=>{console.log(`  ${ok?'✓':'✗'} ${n}${ok||!d?'':' — '+d}`); if(!ok) failed=true;};
const errors=[];
const page = await browser.newPage({ viewport:{width:900,height:800} });
await page.addInitScript(() => {
  window.__sent=[]; const sockets=[];
  class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';sockets.push(this);
    setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
   send(d){window.__sent.push(d);} close(){this.readyState=3;this.onclose&&this.onclose({});}}
  FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
  window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
  // controllable /upload: each call parks until __finishUpload(ok) is called
  window.__uploads=[];
  const realFetch = window.fetch.bind(window);
  window.fetch = (u, o) => {
    if (!String(u).includes('/upload')) return realFetch(u, o);
    return new Promise(res => window.__uploads.push(res));
  };
  window.__finishUpload = (ok, path) => { const r = window.__uploads.shift(); if (!r) return false;
    r(ok ? { ok:true, status:200, json: async()=>({ path, name:'shot.png' }) } : { ok:false, status:413 }); return true; };
  try{localStorage.clear();}catch{}
  for (const m of ['clawd-atg'])
    try{localStorage.setItem('cc_e2e_rs_'+m, JSON.stringify({id:'p-'+m,master:'AAAA',exp:Date.now()+3600e3}));}catch{}
});
page.on('pageerror',e=>errors.push(String(e)));
page.route('https://fleet.probe/', r=>r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:fleetHtml}));
const CID='cid-probe-1';
async function boot() {
  await page.goto('https://fleet.probe/',{waitUntil:'domcontentloaded'});

await page.waitForTimeout(500);
await page.evaluate(()=>{ window.__relayRx({type:'prefs',inactive:[],irons:[]});
  window.__relayRx({type:'machines',machines:[{id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:1,sessions:1,active:0}}]});
  handleMachineJson('clawd-atg',{type:'projects',projects:[
    {pid:'p1',name:'alpha',repoUrl:'https://github.com/clawdbotatg/alpha',kind:'gh',status:'ready',sessionCount:1,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:100,emoji:''}]});
  handleMachineJson('clawd-atg',{type:'sessions',sessions:[
    {cid:'cid-probe-1',pid:'p1',title:'probe upload',desc:'',promptCount:2,alive:true,busy:false,autopilot:false,pilotStatus:'',pilotRounds:0,lastActive:Date.now()/1000,promptedAt:Date.now()/1000}]});
});
await page.waitForTimeout(300);
const CID='cid-probe-1';
await page.evaluate((CID)=>{ window.__frames=[]; hsend=(f)=>{window.__frames.push(f);return true;};
  location.hash = '#/m/clawd-atg/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id) + '/s/' + CID + '/tty'; }, CID);
await page.waitForTimeout(600);
await page.evaluate((CID)=>{ currentCid=CID; currentMachine='clawd-atg'; }, CID);
check('landed in the tty view', await page.evaluate(()=>currentView()==='tty'));
check('composer visible', await page.evaluate(()=>!!box && box.offsetParent !== null));

  await page.evaluate((CID)=>{ window.__frames=[]; hsend=(f)=>{window.__frames.push(f);return true;};
    location.hash = '#/m/clawd-atg/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id) + '/s/' + CID + '/tty'; }, CID);
  await page.waitForTimeout(600);
  await page.evaluate((CID)=>{ currentCid=CID; currentMachine='clawd-atg'; }, CID);
}
await page.addInitScript(() => { if (!sessionStorage.getItem('__booted')) { try{localStorage.clear();}catch{} sessionStorage.setItem('__booted','1'); } });
await boot();
check('landed in the tty view', await page.evaluate(()=>currentView()==='tty'));

const sends = () => page.evaluate(()=>window.__frames.filter(f=>f.type==='send'));
const boxes = () => page.evaluate(()=>[...document.querySelectorAll('#pending .pending-msg')].map(e=>({ tag:e.querySelector('.tag').textContent, body:e.querySelector('div').textContent, resend:!!e.querySelector('button.resend') })));
const ack = (m) => page.evaluate((m)=>handleMachineJson('clawd-atg', m), m);
const land = (text) => page.evaluate(({CID,text})=>handleMachineJson('clawd-atg', {type:'transcript', cid:CID, event:{role:'user', text}}), {CID,text});
const type = async (t) => { await page.click('#box'); await page.keyboard.type(t); await page.keyboard.press('Enter'); await page.waitForTimeout(150); };

// ── 1-2. id + receipt ────────────────────────────────────────────────────────
await type('first message');
let s = await sends();
check('1. send frame carries an id', s.length===1 && !!s[0].id, JSON.stringify(s));
let b = await boxes();
check('1. box reads ⏳ sending', b.length===1 && /sending/.test(b[0].tag), JSON.stringify(b));
await ack({type:'sendAck', id:s[0].id, cid:CID}); await page.waitForTimeout(50);
b = await boxes();
check('2. receipt → ✓ on atg · waiting', b.length===1 && /✓ on atg · waiting/.test(b[0].tag), JSON.stringify(b));

// ── 3. reload keeps the ✓ box, composer stays empty ──────────────────────────
await page.reload({waitUntil:'domcontentloaded'}); await page.waitForTimeout(300);
await boot(); await page.waitForTimeout(200);
b = await boxes();
check('3. after reload: the ✓ box is back', b.length===1 && /✓ on atg/.test(b[0].tag) && b[0].body==='first message', JSON.stringify(b));
check('3. after reload: composer is empty', await page.evaluate(()=>box.value===''), await page.evaluate(()=>box.value));

// ── 4. landing clears it ─────────────────────────────────────────────────────
await land('first message'); await page.waitForTimeout(50);
check('4. landed → box gone', (await boxes()).length===0, JSON.stringify(await boxes()));
check('4. landed → outbox + receipt cleared', await page.evaluate((CID)=>!sessionStorage.getItem(outboxKey(CID)) && !sessionStorage.getItem(outackKey(CID)), CID));

// ── 5. no receipt → ⚠ + resend ───────────────────────────────────────────────
await page.evaluate(()=>{ window.__frames=[]; });
await type('lost one');
const firstId = (await sends())[0].id;
await page.waitForTimeout(12400);
b = await boxes();
check('5. no receipt → ⚠ not delivered + resend', b.length===1 && /not delivered/.test(b[0].tag) && b[0].resend, JSON.stringify(b));
await page.click('#pending button.resend'); await page.waitForTimeout(100);
s = await sends();
check('5. resend → a second frame, fresh id', s.length===2 && s[1].text==='lost one' && s[1].id && s[1].id!==firstId, JSON.stringify(s));
b = await boxes();
check('5. …one box, back to ⏳ sending', b.length===1 && /sending/.test(b[0].tag), JSON.stringify(b));
await ack({type:'sendAck', id:firstId, cid:CID}); await page.waitForTimeout(50);
check('5. late receipt for the first try leaves the new box alone', /sending/.test((await boxes())[0].tag), JSON.stringify(await boxes()));
await ack({type:'sendAck', id:s[1].id, cid:CID}); await page.waitForTimeout(50);
check('5. the resend\'s own receipt → ✓', /✓ on atg/.test((await boxes())[0].tag), JSON.stringify(await boxes()));
await land('lost one');

// ── 6. error receipt ─────────────────────────────────────────────────────────
await page.evaluate(()=>{ window.__frames=[]; });
await type('to nowhere');
await ack({type:'sendAck', id:(await sends())[0].id, error:'no session'}); await page.waitForTimeout(50);
check('6. error receipt → ⚠ at once', /not delivered/.test((await boxes())[0].tag), JSON.stringify(await boxes()));
await land('to nowhere');

// ── 6b. lands while you're on another session → no ghost box coming back ─────
await page.evaluate(()=>{ window.__frames=[]; });
await type('landed elsewhere');
await ack({type:'sendAck', id:(await sends())[0].id, cid:CID}); await page.waitForTimeout(50);
await page.evaluate(()=>subscribe('cid-other'));
await land('landed elsewhere');
await page.evaluate((CID)=>subscribe(CID), CID); await page.waitForTimeout(50);
check('6b. landed while away → no box on return', (await boxes()).length===0, JSON.stringify(await boxes()));

// ── 7. unreceived + reload → text back in the box (old net still works) ──────
await page.evaluate(()=>{ window.__frames=[]; });
await type('unconfirmed');
await page.reload({waitUntil:'domcontentloaded'}); await page.waitForTimeout(300);
await boot(); await page.waitForTimeout(200);
check('7. no receipt + reload → text back in the composer', await page.evaluate(()=>box.value==='unconfirmed'), await page.evaluate(()=>box.value));

check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed ? 'sendackprobe: FAIL' : 'sendackprobe: OK');
process.exit(failed ? 1 : 0);
