// fleetrebootprobe — fleet mode: after a harness restart ("Updating…" overlay),
// the page must come back by itself as soon as the box answers with a new boot
// id — not sit on the overlay until the 25s safety net (Austin, 11-14: "it says
// reconnecting automatically but never reconnects").
// Fleet mode + stubbed relay WebSocket: no server, no session.
//   cd tools && node fleetrebootprobe.mjs
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = dirname(HERE);
function findChromium(){const c=join(process.env.HOME,'Library/Caches/ms-playwright');
 for(const d of readdirSync(c).filter(d=>d.startsWith('chromium_headless_shell-')).sort().reverse())
  for(const a of ['mac-arm64','mac-x64']){const b=join(c,d,`chrome-headless-shell-${a}`,'chrome-headless-shell');if(existsSync(b))return b;}}
const fleetHtml = readFileSync(join(ROOT,'index.html'),'utf8').replace('<head>','<head><script>window.__FLEET__=true;</script>');
const browser = await chromium.launch({ executablePath: findChromium() });
let failed=false; const check=(n,ok,d)=>{console.log(`  ${ok?'✓':'✗'} ${n}${ok||!d?'':' — '+d}`); if(!ok) failed=true;};
const page = await browser.newPage({ viewport:{width:1000,height:700} });
await page.addInitScript(() => {
  const sockets=[];
  class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';sockets.push(this);
    setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
   send(){} close(){this.readyState=3;this.onclose&&this.onclose({});}}
  FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
  window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
});
let loads = 0;
await page.route('https://fleet.probe/', r=>{ loads++; r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:fleetHtml}); });
await page.goto('https://fleet.probe/',{waitUntil:'domcontentloaded'});
await page.waitForTimeout(500);
const proj = (boot) => ({type:'projects',boot,projects:[{pid:'p1',name:'alpha',kind:'gh',status:'ready',sessionCount:0,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:100,emoji:''}]});
await page.evaluate((f)=>{
  window.__relayRx({type:'prefs',inactive:[],irons:[]});
  window.__relayRx({type:'machines',machines:[
    {id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{}},
    {id:'clawd-sat',host:'sat',kind:'machine',online:true,lastSeen:0,stats:{}}]});
  currentMachine = 'clawd-atg';
  handleMachineJson('clawd-atg', f);
  handleMachineJson('clawd-sat', {...f, boot:'sat1'});
}, proj('boot1'));
// same boot again (a fresh harness link, no restart) and ANOTHER box restarting: no reload
await page.evaluate((f)=>{ handleMachineJson('clawd-atg', f); handleMachineJson('clawd-sat', {...f, boot:'sat2'}); }, proj('boot1'));
await page.waitForTimeout(1800);
check('no reload without a restart of the viewed box', loads === 1, 'loads=' + loads);
check('overlay stays down', await page.evaluate(()=>document.getElementById('rebootovl').hidden));
// the viewed box restarts: go → overlay; it answers with a new boot id → reload
await page.evaluate(()=>handleMachineJson('clawd-atg',{type:'restart',state:'go'}));
check('go shows the overlay', await page.evaluate(()=>!document.getElementById('rebootovl').hidden));
await page.waitForTimeout(1500);
check('still waiting while the box is down', loads === 1, 'loads=' + loads);
await page.evaluate((f)=>handleMachineJson('clawd-atg', f), proj('boot2'));
await page.waitForTimeout(2500);
check('new boot id reloads the page (well before 25s)', loads === 2, 'loads=' + loads);
check('overlay gone after the reload', await page.evaluate(()=>document.getElementById('rebootovl').hidden));
await browser.close();
if (failed) { console.error('FAIL'); process.exit(1); }
console.log('ok — fleet page comes back by itself after a harness restart');
