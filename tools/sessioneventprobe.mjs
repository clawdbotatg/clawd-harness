// sessioneventprobe — a session is terminal-only (2026-11 audit U2). The old
// chat-bubble view could not be opened, yet every transcript line was still
// built into a hidden bubble, typed out on a timer, under a hidden spinner.
// Contract pinned:
//   1. no hidden session feed exists, and a transcript line builds no bubble;
//   2. the side jobs that code did still happen: a landed prompt drops its
//      queued box (user AND box-sent slash command), and live assistant prose
//      is read aloud — not history, not with 🟦 on, not for another session;
//   3. a busy turn starts no spinner timers for the session.
// Fleet mode + stubbed relay WebSocket + stubbed hsend (sendackprobe setup).
//   cd tools && node sessioneventprobe.mjs
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
  window.__spoken=[]; Object.defineProperty(window,'speechSynthesis',{configurable:true,value:{speak(u){window.__spoken.push(u.text);},cancel(){},speaking:false,pending:false}});
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


const ev = (event, extra={}) => page.evaluate(({CID,event,extra})=>handleMachineJson('clawd-atg', {type:'transcript', cid:CID, event, ...extra}), {CID,event,extra});
const spoken = () => page.evaluate(()=>window.__spoken.splice(0));
const nboxes = () => page.evaluate(()=>document.querySelectorAll('#pending .pending-msg').length);
const type = async (t) => { await page.click('#box'); await page.keyboard.type(t); await page.keyboard.press('Enter'); await page.waitForTimeout(150); };
await page.evaluate(()=>{ serverTts = false; setTts(true); });

check('1. no hidden session feed in the page', await page.evaluate(()=>!document.getElementById('feed') && !document.getElementById('right')));
const timers0 = await page.evaluate(()=>{ window.__iv=0; const si=window.setInterval; window.setInterval=(f,ms,...a)=>{ if (ms<=100) window.__iv++; return si(f,ms,...a); }; return 0; });

await type('hello there');
check('2. a send shows its queued box', await nboxes()===1);
await ev({role:'user', text:'hello there'}); await page.waitForTimeout(80);
check('2. the prompt landing drops the box', await nboxes()===0);
await type('/compact ');
const had = await nboxes();
await ev({role:'command', text:'/compact'}); await page.waitForTimeout(80);
check('2. a box-sent slash command landing drops its box', had===1 && await nboxes()===0, 'had='+had);

await spoken();
await ev({role:'assistant', text:'All **done**. Shipped it.', tools:[{name:'Bash', input:{command:'ls'}}]}); await page.waitForTimeout(80);
let sp = await spoken();
check('2. live assistant prose is read aloud', sp.join(' ').includes('All done') && sp.join(' ').includes('Shipped it'), JSON.stringify(sp));
await ev({role:'assistant', text:'Old answer.'}, {history:true}); await page.waitForTimeout(50);
check('2. history replay is not read aloud', (await spoken()).length===0);
await page.evaluate(()=>handleMachineJson('clawd-atg', {type:'transcript', cid:'cid-other', event:{role:'assistant', text:'Someone else.'}})); await page.waitForTimeout(50);
check('2. another session is not read aloud', (await spoken()).length===0);
await page.evaluate(()=>{ tldrOn = true; });
await ev({role:'assistant', text:'Long answer.'}); await page.waitForTimeout(50);
check('2. with 🟦 on the raw answer is not read (the summary is)', (await spoken()).length===0);
await page.evaluate(()=>{ tldrOn = false; });
await ev({role:'tool_result', results:['x'.repeat(500)]}); await ev({role:'result', subtype:'success'}); await ev({role:'system', text:'note'});
await page.waitForTimeout(700);
check('1. transcript lines build no bubbles', await page.evaluate(()=>document.querySelectorAll('.msg').length===0));

await page.evaluate((CID)=>{ window.__iv=0; handleMachineJson('clawd-atg',{type:'hook',cid:CID,event:'PreToolUse',data:{tool:'Bash'},busy:true}); }, CID);
await page.waitForTimeout(100);
check('3. a busy turn sets the pill', await page.evaluate(()=>document.getElementById('status').classList.contains('working')));
check('3. …and starts no spinner timer', await page.evaluate(()=>window.__iv===0), String(await page.evaluate(()=>window.__iv)));
check('3. no thinking row outside the PM chat', await page.evaluate(()=>!document.querySelector('.thinking')));
await page.evaluate((CID)=>handleMachineJson('clawd-atg',{type:'hook',cid:CID,event:'Stop',data:{},busy:false}), CID);
check('3. Stop → idle', await page.evaluate(()=>document.getElementById('status').classList.contains('idle')));

check('no page errors', errors.length===0, errors.join(' | '));
await page.screenshot({ path: join(HERE,'sessioneventprobe.png') });
await browser.close();
console.log(failed ? '\nFAIL' : '\nall green');
process.exit(failed?1:0);
