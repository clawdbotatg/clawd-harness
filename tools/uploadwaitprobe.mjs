// uploadwaitprobe — Enter while an attachment is still uploading (2026-09-04).
// The bug: paste an image, type, hit Enter before the upload lands → the text
// went alone and the image was orphaned (sendMessage folded in only attachments
// that already had a path, then wiped the list). Contract pinned:
//   1. Enter mid-upload sends NOTHING yet; the box empties; an amber
//      "📎 uploading…" box appears in #pending; the chip strip is cleared;
//   2. the composer is free — typing a second message during the wait sticks;
//   3. when the upload lands, ONE send frame goes out: text + the path, the
//      held box becomes the normal queued box, the second draft is untouched;
//   4. a FAILED upload puts the message back in the box with its ⚠ chip — the
//      text never goes out without its image;
//   5. ✕ on the held box cancels the same way;
//   6. no pending upload → Enter sends immediately, as before.
// Fleet mode + stubbed relay WebSocket + stubbed fetch('/upload') (tldrprobe
// pattern): no server, no session, no real upload. Desktop viewport so Enter
// submits (on touch Enter is a newline by design).
//   cd tools && node uploadwaitprobe.mjs
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
await page.route('https://fleet.probe/', r=>r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:fleetHtml}));
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

const sends = () => page.evaluate(()=>window.__frames.filter(f=>f.type==='send'));
const held = () => page.evaluate(()=>{ const h=document.querySelector('.pending-msg.hold'); return h ? { tag:h.querySelector('.tag').textContent, body:h.querySelector('div').textContent } : null; });
const queued = () => page.evaluate(()=>[...document.querySelectorAll('.pending-msg:not(.hold)')].map(e=>e.querySelector('div').textContent));
const paste = () => page.evaluate(()=>{ uploadFile(new File([new Uint8Array(64)], 'shot.png', { type:'image/png' })); });

// ── 1. Enter mid-upload holds ────────────────────────────────────────────────
await paste(); await page.waitForTimeout(100);
check('chip shows uploading…', await page.evaluate(()=>/uploading/.test(document.querySelector('#attachments .chip.up')?.textContent||'')));
await page.click('#box'); await page.keyboard.type('look at this'); await page.keyboard.press('Enter');
await page.waitForTimeout(150);
check('1. nothing sent yet', (await sends()).length === 0, JSON.stringify(await sends()));
check('1. box emptied', await page.evaluate(()=>box.value==='') );
let h = await held();
check('1. held box shows the text, tagged uploading', !!h && /uploading/.test(h.tag) && h.body==='look at this', JSON.stringify(h));
check('1. chip strip cleared', await page.evaluate(()=>document.querySelectorAll('#attachments .chip').length===0));

// ── 2. composer free during the wait ─────────────────────────────────────────
await page.keyboard.type('second thought');
check('2. second draft sticks while held', await page.evaluate(()=>box.value==='second thought'));

// ── 3. upload lands → one send: text + path ──────────────────────────────────
await page.evaluate(()=>window.__finishUpload(true, '/tmp/up/shot.png'));
await page.waitForTimeout(200);
let s = await sends();
check('3. exactly one send frame', s.length===1, JSON.stringify(s));
check('3. frame carries text + path', s.length===1 && s[0].text==='look at this /tmp/up/shot.png', s[0]&&s[0].text);
check('3. held box gone, queued box in its place', (await held())===null && (await queued()).includes('look at this'), JSON.stringify(await queued()));
check('3. second draft untouched', await page.evaluate(()=>box.value==='second thought'));
await page.evaluate(()=>{ box.value=''; window.__frames=[]; clearPending(); });

// ── 4. failed upload → back in the box with the ⚠ chip ───────────────────────
await paste(); await page.waitForTimeout(100);
await page.click('#box'); await page.keyboard.type('with a broken one'); await page.keyboard.press('Enter');
await page.waitForTimeout(100);
check('4. held while uploading', (await held())!==null);
await page.evaluate(()=>window.__finishUpload(false));
await page.waitForTimeout(200);
check('4. nothing sent', (await sends()).length===0, JSON.stringify(await sends()));
check('4. text back in the box', await page.evaluate(()=>box.value==='with a broken one'), await page.evaluate(()=>box.value));
check('4. ⚠ chip back on the strip', await page.evaluate(()=>!!document.querySelector('#attachments .chip.err')));
check('4. held box gone', (await held())===null);
await page.evaluate(()=>{ box.value=''; attachments=[]; renderChips(); window.__frames=[]; clearPending(); });

// ── 5. ✕ cancels back into the box ───────────────────────────────────────────
await paste(); await page.waitForTimeout(100);
await page.click('#box'); await page.keyboard.type('never mind'); await page.keyboard.press('Enter');
await page.waitForTimeout(100);
await page.click('.pending-msg.hold button');
await page.waitForTimeout(100);
check('5. ✕ restores text', await page.evaluate(()=>box.value==='never mind'));
check('5. ✕ restores the uploading chip', await page.evaluate(()=>!!document.querySelector('#attachments .chip.up')));
await page.evaluate(()=>window.__finishUpload(true, '/tmp/up/late.png'));
await page.waitForTimeout(200);
check('5. late-landing upload after cancel sends nothing', (await sends()).length===0);
check('5. …but the chip is now ready with its path', await page.evaluate(()=>attachments.length===1 && attachments[0].path==='/tmp/up/late.png'));
await page.evaluate(()=>{ box.value=''; attachments=[]; renderChips(); window.__frames=[]; clearPending(); });

// ── 6. no pending upload → immediate send, as before ─────────────────────────
await page.click('#box'); await page.keyboard.type('plain'); await page.keyboard.press('Enter');
await page.waitForTimeout(100);
s = await sends();
check('6. plain Enter sends immediately', s.length===1 && s[0].text==='plain', JSON.stringify(s));
check('6. no held box', (await held())===null);

check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed ? 'uploadwaitprobe: FAIL' : 'uploadwaitprobe: OK');
process.exit(failed ? 1 : 0);
