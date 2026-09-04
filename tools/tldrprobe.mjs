// tldrprobe — the 🟦 live TLDR block (2026-09-04): the blue plain-English
// summary floating over the terminal while claude writes. Contract pinned:
//   1. off by default: a tldr frame with the toggle off shows nothing;
//   2. tapping 🟦 (real touch) turns it on, persists, and sends {type:'tldr',on:true};
//   2b. the row is a FIXED-height footer slot whenever the mode is on — its height
//       must not change between passes (a footer resize = PTY resize + replay storm);
//   3. a tldr frame for the VIEWED session paints the block (live = "updating…");
//   4. a later frame REPLACES the text (rolling summary), final drops "updating";
//   5. a frame for another session is ignored;
//   6. an empty-text frame (new prompt) blanks it; so does UserPromptSubmit;
//   7. typing in the composer KEEPS it; sending clears it (you're done reading);
//   8. it sits in the footer directly above the session-name row (#descrow), as
//      light-blue text on the normal background — not a box over the terminal;
//   9. subscribe re-asserts the preference (tldr frame right after subscribe).
// Fleet mode + stubbed relay WebSocket (tapprobe pattern): no server, no session.
//   cd tools && node tldrprobe.mjs
import { chromium, devices } from 'playwright-core';
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
const iphone = devices['iPhone 12'];
const page = await browser.newPage({ ...iphone, viewport:{width:390,height:844} });
await page.addInitScript(() => {
  window.__sent=[]; const sockets=[];
  class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';sockets.push(this);
    setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
   send(d){window.__sent.push(d);} close(){this.readyState=3;this.onclose&&this.onclose({});}}
  FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
  window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
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
    {cid:'cid-probe-1',pid:'p1',title:'probe tldr',desc:'',promptCount:2,alive:true,busy:true,autopilot:false,pilotStatus:'',pilotRounds:0,lastActive:Date.now()/1000,promptedAt:Date.now()/1000}]});
});
await page.waitForTimeout(300);
// land on alpha's sessions rung, where the composer (and the 🟦 toggle) lives
await page.evaluate(()=>{ location.hash = '#/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id); });
await page.waitForTimeout(400);
const CID='cid-probe-1';

// stub hsend so the toggle/subscribe frames are observable; pretend we're viewing the session
// open the session's tty view for real (the row that holds 🤖 + 🟦 only lays out there)
await page.evaluate((CID)=>{ window.__frames=[]; hsend=(f)=>{window.__frames.push(f);return true;};
  location.hash = '#/m/clawd-atg/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id) + '/s/' + CID + '/tty'; }, CID);
await page.waitForTimeout(600);
await page.evaluate((CID)=>{ currentCid=CID; renderPilotUI(); }, CID);
check('landed in the tty view', await page.evaluate(()=>currentView()==='tty'));

// 1. off by default
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'should not show',final:false}), CID);
check('off by default: frame shows nothing', await page.evaluate(()=>tldrEl.hidden && !tldrOn));
const gone = () => page.evaluate(()=>tldrTextEl.textContent==='' && tldrEl.classList.contains('empty'));

// 2. tap 🟦 (real touch)
const cdp = await page.context().newCDPSession(page);
const xy = await page.evaluate(()=>{const r=tldrBtn.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,visible:r.width>0&&r.height>0};});
check('🟦 toggle visible, left of the 🤖 checkbox', xy.visible && await page.evaluate(()=>tldrBtn.nextElementSibling===pilotChk && !pilotChk.hidden), JSON.stringify(xy));
await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:xy.x,y:xy.y}]});
await page.waitForTimeout(80);
await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
await page.waitForTimeout(200);
const on = await page.evaluate((CID)=>({on:tldrOn, cls:tldrBtn.classList.contains('on'), ls:localStorage.getItem('cc_tldr'),
  frame:window.__frames.find(f=>f.type==='tldr'&&f.cid===CID&&f.on===true)}), CID);
check('tap turns it on + persists + sends the verb', on.on && on.cls && on.ls==='1' && !!on.frame, JSON.stringify(on));
const h0 = await page.evaluate(()=>({hidden:tldrEl.hidden, h:tldrEl.getBoundingClientRect().height, footer:document.querySelector('footer').getBoundingClientRect().height}));
check('mode on: the empty slot is already in the footer', !h0.hidden && h0.h>40, JSON.stringify(h0));

// 3. a frame paints the block, live
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'Fixed the bug. Tests pass.',final:false}), CID);
let st = await page.evaluate(()=>({hidden:tldrEl.hidden, text:tldrText.textContent, live:tldrEl.classList.contains('live'),
  pos:getComputedStyle(tldrEl).position, parent:tldrEl.parentElement.tagName, next:tldrEl.nextElementSibling&&tldrEl.nextElementSibling.id,
  color:getComputedStyle(tldrEl).color, bg:getComputedStyle(tldrEl).backgroundColor,
  above: tldrEl.getBoundingClientRect().bottom <= descrow.getBoundingClientRect().top + 1}));
check('frame paints the row, marked updating', !st.hidden && st.text==='Fixed the bug. Tests pass.' && st.live, JSON.stringify(st));
const h1 = await page.evaluate(()=>({h:tldrEl.getBoundingClientRect().height, footer:document.querySelector('footer').getBoundingClientRect().height}));
check('painting text did not change the row or footer height', h1.h===h0.h && h1.footer===h0.footer, JSON.stringify({h0,h1}));
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:('Long line of summary text. ').repeat(40),final:false}), CID);
const h2 = await page.evaluate(()=>({h:tldrEl.getBoundingClientRect().height, footer:document.querySelector('footer').getBoundingClientRect().height, scrolls:tldrEl.scrollHeight>tldrEl.clientHeight}));
check('a long summary scrolls inside; footer height still unchanged', h2.h===h0.h && h2.footer===h0.footer && h2.scrolls, JSON.stringify({h0,h2}));
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'Fixed the bug. Tests pass.',final:false}), CID);
check('sits in the footer right above the session-name row', st.pos==='static' && st.parent==='FOOTER' && st.next==='descrow' && st.above, JSON.stringify(st));
check('light-blue text on the normal background', st.color==='rgb(74, 158, 255)' && st.bg==='rgba(0, 0, 0, 0)', JSON.stringify(st));

// 4. a later frame replaces the text; final drops "updating"
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'Fixed the bug. Tests pass. Pushed.',final:true}), CID);
st = await page.evaluate(()=>({text:tldrText.textContent, live:tldrEl.classList.contains('live')}));
check('rolling: later frame replaces, final = settled', st.text==='Fixed the bug. Tests pass. Pushed.' && !st.live, JSON.stringify(st));

// 5. another session's frame is ignored
await page.evaluate(()=>handleJson({type:'tldr',cid:'cid-other',text:'WRONG SESSION',final:true}));
check('other session ignored', await page.evaluate(()=>tldrText.textContent==='Fixed the bug. Tests pass. Pushed.'));

// 6. empty text = new turn → blank; UserPromptSubmit too
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'',final:false}), CID);
check('empty frame blanks it', await gone());
await page.evaluate((CID)=>{handleJson({type:'tldr',cid:CID,text:'again',final:false}); handleJson({type:'hook',cid:CID,event:'UserPromptSubmit',busy:true,waiting:false,tool:null,data:{prompt:'x'}});}, CID);
check('UserPromptSubmit blanks it', await gone());

// 7. typing keeps it; sending clears it
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'read me',final:true}), CID);
check('shown before typing', await page.evaluate(()=>tldrTextEl.textContent==='read me'));
await page.evaluate(()=>{ box.focus(); });
await page.keyboard.type('ok');
check('typing keeps it on screen', await page.evaluate(()=>tldrTextEl.textContent==='read me' && box.value==='ok'));
await page.evaluate((CID)=>deliverSend(CID, 'ok', null, 'typed'), CID);
check('sending clears it', await gone());
await page.evaluate(()=>{ box.value=''; });

// 7b. switching to another session blanks it — the summary must not follow you
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'stay here',final:true}), CID);
check('shown before the switch', await page.evaluate(()=>tldrTextEl.textContent==='stay here'));
await page.evaluate(()=>{ subscribe('cid-other'); });
check('switching sessions blanks the summary', (await gone()) && await page.evaluate(()=>currentCid==='cid-other'));
await page.evaluate(()=>handleJson({type:'tldr',cid:'cid-probe-1',text:'late frame for the OLD session',final:true}));
check("the old session's late frame doesn't paint", await gone());
await page.evaluate((CID)=>{ subscribe(CID); }, CID);

// 8. subscribe re-asserts the preference
await page.evaluate((CID)=>{ window.__frames.length=0; subscribe(CID); }, CID);
const sub = await page.evaluate(()=>window.__frames.map(f=>f.type));
check('subscribe is followed by the tldr verb', sub[0]==='subscribe' && sub[1]==='tldr', JSON.stringify(sub));

// 8b. tapping the summary marks it read: hides + sends the mark
await page.evaluate((CID)=>{ handleJson({type:'tldr',cid:CID,text:'read me now',final:false}); window.__frames.length=0; }, CID);
const txy = await page.evaluate(()=>{const r=tldrEl.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};});
await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:txy.x,y:txy.y}]});
await page.waitForTimeout(80);
await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
await page.waitForTimeout(200);
check('tap on the summary blanks it + sends mark', (await gone()) && await page.evaluate((CID)=>window.__frames.some(f=>f.type==='tldr'&&f.cid===CID&&f.mark===true), CID));

// 9. off again hides + sends off
await page.evaluate((CID)=>{handleJson({type:'tldr',cid:CID,text:'bye',final:true}); window.__frames.length=0; setTldr(false);}, CID);
check('off hides the slot + sends the verb', await page.evaluate(()=>tldrEl.hidden && !tldrOn && window.__frames.some(f=>f.type==='tldr'&&f.on===false)));

check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
if (failed) { console.log('tldrprobe: FAIL'); process.exit(1); } else console.log('tldrprobe: all green');
