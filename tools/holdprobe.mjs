// holdprobe — two "the screen ate it" fixes from the 2026-11 audit (U3, U4):
//   U3. you hit send while a photo is still uploading, then walk to another
//       session; the upload fails (or you cancel). The words used to be gone:
//       the box was emptied at the send, navigation dropped the empty draft,
//       and the restore bailed because "you've moved on". They must be in that
//       session's draft when you come back. And a NEW message typed in the box
//       during the upload must survive the send.
//   U4. ＋ / ⑂ / ↩ with the socket down must not blank the terminal.
// Fleet mode + stubbed relay WebSocket + stubbed hsend — never a real session.
//   cd tools && node holdprobe.mjs
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
  if (!sessionStorage.getItem('__booted')) { try{localStorage.clear();}catch{} sessionStorage.setItem('__booted','1'); }   // first load only — a reload must keep what the page saved
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
await boot();
check('landed in the tty view', await page.evaluate(()=>currentView()==='tty'));


const hash = (tail) => page.evaluate((tail)=>{ location.hash = '#/m/clawd-atg/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id) + tail; }, tail);
const hold = (text) => page.evaluate((text)=>{
  const a = { name:'shot.png', pending:true }; a.done = new Promise(r => { window.__finish = (o) => { Object.assign(a, o, {pending:false}); r(); }; });
  holdSend(text, [a]); }, text);
const finish = async (o) => { await page.evaluate((o)=>window.__finish(o), o); await page.waitForTimeout(150); };
const draft = (id) => page.evaluate((id)=>localStorage.getItem(draftKey(id)), id);
const back = async () => { await hash('/s/'+CID+'/tty'); await page.waitForTimeout(500); await page.evaluate((CID)=>{ currentCid=CID; currentMachine='clawd-atg'; syncComposer && syncComposer(); }, CID); await page.waitForTimeout(150); };

// ── U3a. upload fails after you left ─────────────────────────────────────────
await page.evaluate(()=>{ box.value='lost words?'; });
await hold('lost words?');
check('the hold empties the box', await page.evaluate(()=>box.value==='' ));
await hash(''); await page.waitForTimeout(500);
check('walked to the sessions rung', await page.evaluate(()=>currentView()==='sessions'));
await finish({ error:true });
check('failed upload after leaving: the words are in that session\'s draft', await draft(CID)==='lost words?', String(await draft(CID)));
await back();
check('…and in the box when you come back', await page.evaluate(()=>box.value==='lost words?'), await page.evaluate(()=>box.value));
check('nothing was sent', (await page.evaluate(()=>window.__frames.filter(f=>f.type==='send').length))===0);

// ── U3b. cancel (✕) after you left ───────────────────────────────────────────
await page.evaluate(()=>{ box.value=''; saveDraft(); });
await hold('cancelled words');
await hash(''); await page.waitForTimeout(500);
await page.evaluate(()=>document.querySelector('#pending .pending-msg.hold button')?.click());
await page.waitForTimeout(100);
check('cancel after leaving keeps the words too', (await draft(CID))==='cancelled words', String(await draft(CID)));
await finish({ error:true });
check('…and the late upload result does not double them', (await draft(CID))==='cancelled words', String(await draft(CID)));
await back();

// ── U3c. a new message typed during the upload survives the send ─────────────
await page.evaluate(()=>{ box.value=''; saveDraft(); window.__frames=[]; });
await hold('first one');
await page.click('#box'); await page.keyboard.type('second thought');
await finish({ path:'/tmp/shot.png' });
check('the held message went out', (await page.evaluate(()=>window.__frames.filter(f=>f.type==='send').length))===1);
check('the new text is still in the box', await page.evaluate(()=>box.value==='second thought'));
check('…and still saved as the draft', (await draft(CID))==='second thought', String(await draft(CID)));
await page.evaluate(()=>{ box.value=''; saveDraft(); clearPending(); });

// ── U4. socket down: ＋ / ⑂ leave the screen alone ───────────────────────────
await page.evaluate(()=>{ window.__resets=0; const r=term.reset.bind(term); term.reset=()=>{ window.__resets++; r(); };
  hsend=()=>false; hsendTo=()=>false; });
await page.evaluate(()=>newSession());
check('＋ offline: terminal not blanked', (await page.evaluate(()=>window.__resets))===0);
check('＋ offline: still on the same session', await page.evaluate((CID)=>currentCid===CID && !pendingNewFocus, CID));
check('＋ offline: says why', await page.evaluate(()=>/not connected/.test(meta.textContent)));
await page.evaluate(()=>{ meta.textContent=''; forkSession(); });
check('⑂ offline: terminal not blanked, same session', await page.evaluate((CID)=>window.__resets===0 && currentCid===CID && !pendingNewFocus, CID));
await page.evaluate(()=>{ meta.textContent=''; reopenClosed({ cid:'old-1', pid:'p1', machine:'clawd-atg' }); });
check('↩ offline: terminal not blanked, same session', await page.evaluate((CID)=>window.__resets===0 && currentCid===CID && !pendingNewFocus, CID),
  await page.evaluate(()=>JSON.stringify({r:window.__resets,c:currentCid,m:meta.textContent})));
await page.evaluate(()=>{ window.__frames=[]; hsend=(f)=>{window.__frames.push(f);return true;}; newSession(); });
check('＋ online still blanks and asks', await page.evaluate(()=>window.__resets===1 && currentCid===null && window.__frames.some(f=>f.type==='new')));

check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed ? 'holdprobe: FAIL' : 'holdprobe: all green');
process.exit(failed ? 1 : 0);
