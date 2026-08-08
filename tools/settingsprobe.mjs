// settingsprobe — drive the ⚙️ settings page in FLEET mode and assert the
// "default machine for new projects" select actually decides where a new repo
// lands.
//
//   cd tools && node settingsprobe.mjs
//
// This setting used to be a "default for new repos" checkbox on every 🖥️ machine
// card: a one-of-N answer spread across N boxes that each had to clear the
// others, and invisible unless you went looking on the roster. It's now one
// select in settings — so the thing to guard is the CHAIN, not the widget:
// picking a machine must (a) persist, (b) re-label the projects rung's
// 'default/all' option, and (c) be what ensureTargetMachine() actually targets.
// A select that stores a value nothing reads would look perfectly fine.
//
// Needs no relay and no worker: window.WebSocket is stubbed before the page
// loads (same fake-relay trick as fleetprobe.mjs), so nothing leaves this
// machine and no real session is touched. Exit code is non-zero if a check
// fails, so it works in a verify flow.
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = dirname(HERE);
function findChromium(){const c=join(process.env.HOME,'Library/Caches/ms-playwright');
 for(const d of readdirSync(c).filter(d=>d.startsWith('chromium_headless_shell-')).sort().reverse())
  for(const a of ['mac-arm64','mac-x64']){const b=join(c,d,`chrome-headless-shell-${a}`,'chrome-headless-shell');if(existsSync(b))return b;}}
const html = readFileSync(join(ROOT,'index.html'),'utf8').replace('<head>','<head><script>window.__FLEET__=true;</script>');
const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1000, height: 900 } });
await page.addInitScript(() => {
  window.__sent=[]; const sockets=[];
  class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';sockets.push(this);
    setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
   send(d){window.__sent.push(d);} close(){this.readyState=3;this.onclose&&this.onclose({});}}
  FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
  window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
  try{localStorage.clear();}catch{}
  for (const m of ['clawd-atg','clawd-head','clawd-heart'])
    try{localStorage.setItem('cc_e2e_rs_'+m, JSON.stringify({id:'p-'+m,master:'AAAA',exp:Date.now()+3600e3}));}catch{}
});
const errors=[]; page.on('pageerror',e=>errors.push(String(e)));
await page.route('https://fleet.probe/', r => r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:html}));
let failed=false; const check=(n,ok,d)=>{console.log(`  ${ok?'✓':'✗'} ${n}${ok||!d?'':' — '+d}`); if(!ok) failed=true;};
const ROSTER={type:'machines',machines:[
 {id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:2,sessions:3,active:1}},
 {id:'clawd-head',host:'head',kind:'machine',online:true,lastSeen:0,stats:{projects:1,sessions:1,active:0}},
 {id:'clawd-heart',host:'heart',kind:'machine',online:false,lastSeen:0,stats:{projects:1,sessions:0,active:0}},
 {id:'clawd-nerve-cord',host:'hub',kind:'relay',online:true,lastSeen:0,stats:null}]};
await page.goto('https://fleet.probe/',{waitUntil:'domcontentloaded',timeout:15000});
await page.waitForTimeout(600);
await page.evaluate(o=>window.__relayRx(o),{type:'prefs',inactive:[]});
await page.evaluate(o=>window.__relayRx(o),ROSTER);
await page.waitForTimeout(900);

// 1. the checkbox is gone from the machines page
await page.evaluate(()=>window.openMachines());
await page.waitForTimeout(300);
const machText = await page.$eval('#machinelist', e=>e.innerText);
check('no "default for new repos" checkbox left on the machines page', !/default for new repos/i.test(machText));
check('the "talk to this machine" switch survives', /talk to this machine/.test(machText));
await page.evaluate(()=>{document.getElementById('machinesclose').click();});

// 2. the settings select
await page.evaluate(()=>window.openSettings());
await page.waitForTimeout(300);
const row = await page.evaluate(()=>{
  const rows=[...document.querySelectorAll('#settingsbody .setrow')];
  const r=rows.find(x=>/default machine for new projects/.test(x.innerText));
  if(!r) return null;
  const sel=r.querySelector('select');
  return {rows:rows.length, sub:r.querySelector('.set-s').textContent,
          options:[...sel.options].map(o=>[o.value,o.textContent]), value:sel.value, disabled:sel.disabled};
});
console.log('  row:', JSON.stringify(row));
check('a select row exists with auto + every non-relay machine',
      !!row && row.options.length===4 && row.options[0][0]==='' && row.options.some(o=>o[0]==='clawd-heart'), JSON.stringify(row));
check('the offline machine is listed as offline', !!row && /offline/.test(row.options.find(o=>o[0]==='clawd-heart')[1]));
check('the relay is not offered as a host', !!row && !row.options.some(o=>o[0]==='clawd-nerve-cord'));
check('default value is auto', !!row && row.value==='');
check('sub-line names where repos land today', !!row && /right now that's atg/.test(row.sub), row&&row.sub);

// 3. pick one → stored + reflected
await page.selectOption('#settingsbody select', 'clawd-head');
await page.waitForTimeout(300);
const after = await page.evaluate(()=>({
  stored: localStorage.getItem('cc_clone_default'),
  sub: [...document.querySelectorAll('#settingsbody .setrow')].find(x=>/default machine/.test(x.innerText)).querySelector('.set-s').textContent,
  value: document.querySelector('#settingsbody select').value,
}));
check('choosing a machine stores it', after.stored==='clawd-head', JSON.stringify(after));
check('the select keeps the choice after re-render', after.value==='clawd-head', JSON.stringify(after));
check('sub-line follows the choice', /right now that's head/.test(after.sub), after.sub);

// 4. the projects rung label + the actual create target
await page.evaluate(()=>{document.getElementById('settingsclose').click(); navTo('projects');});
await page.waitForTimeout(400);
const rung = await page.evaluate(()=>{
  const s=document.querySelector('#addProject select');
  return s ? s.options[0].textContent : null;
});
check('projects rung shows the new default', !!rung && /new repos → head/.test(rung), String(rung));
const target = await page.evaluate(()=>{ currentMachine=null; ensureTargetMachine(); return currentMachine; });
check('ensureTargetMachine() targets the chosen machine', target==='clawd-head', String(target));

// 5. auto again
await page.evaluate(()=>window.openSettings());
await page.waitForTimeout(200);
await page.selectOption('#settingsbody select', '');
await page.waitForTimeout(200);
const cleared = await page.evaluate(()=>localStorage.getItem('cc_clone_default'));
check('back to auto clears the pin', cleared===null, String(cleared));
await page.screenshot({path:join(HERE,'settingsprobe.png')});
check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed?'FAIL':'PASS');
process.exit(failed?1:0);
