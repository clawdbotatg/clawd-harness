// sttprobe — composer dictation (🎤 push-to-talk) on an emulated iPhone, with
// SpeechRecognition stubbed. Guards the 2026-09-01 clobber bug: recognition
// results landing late (they trail rec.stop()) or from a session stranded in
// the on state used to blindly `box.value = …`, replacing text the user had
// typed since — "text came rushing in and replaced what I was typing".
//
// The contract pinned here:
//   1. mic hold → results write into the box; dictated text is saved as a draft;
//   2. a result trailing the release still lands — IF the box is untouched;
//   3. once the user types, no result may ever replace the box again, and a
//      still-running recognition self-stops the moment it tries;
//   4. a composer context switch (leaving the rung) kills dictation the same way;
//   5. #micBtn carries touch-action:none so a pan can't pointercancel the hold.
//
// Second act (desktop page): SPACE-HOLD push-to-talk in the composer —
//   6. a quick space tap types a normal space, never records;
//   7. holding space past the threshold rolls back the space(s) the key typed
//      (auto-repeat included) and starts dictation; release stops it;
//   8. auto-repeat spaces are eaten while dictating;
//   9. any other key during the wait cancels the pending hold (space kept).
//
// Fleet mode + stubbed relay WebSocket (tapprobe pattern): no real server, no
// real session, no mic. Real touch gestures via CDP, not element.click().
//   cd tools && node sttprobe.mjs
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
const initStub = () => {
  window.__sent=[]; const sockets=[];
  class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';sockets.push(this);
    setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
   send(d){window.__sent.push(d);} close(){this.readyState=3;this.onclose&&this.onclose({});}}
  FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
  window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
  try{localStorage.clear();}catch{}
  for (const m of ['clawd-atg'])
    try{localStorage.setItem('cc_e2e_rs_'+m, JSON.stringify({id:'p-'+m,master:'AAAA',exp:Date.now()+3600e3}));}catch{}
  // stub SpeechRecognition BEFORE the app script sniffs for it
  window.__srs = [];
  window.SpeechRecognition = class {
    constructor(){ this.startedCount=0; this.live=false; window.__srs.push(this); window.__sr=this; }
    start(){ this.startedCount++; this.live=true; }
    stop(){ this.live=false; const f=this.onend; if (f) setTimeout(()=>f(),0); }
    abort(){ this.live=false; }
  };
  // emit a recognition result the way Chrome shapes it: only NEW finals per event
  window.__emit=(finals,interim)=>{
    const results=(finals||[]).map(t=>Object.assign([{transcript:t}],{isFinal:true}));
    if (interim!=null) results.push(Object.assign([{transcript:interim}],{isFinal:false}));
    window.__sr.onresult({resultIndex:0,results});
  };
};
await page.addInitScript(initStub);
page.on('pageerror',e=>errors.push(String(e)));
const bootPage = async (pg) => {
  await pg.route('https://fleet.probe/', r=>r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:fleetHtml}));
  await pg.goto('https://fleet.probe/',{waitUntil:'domcontentloaded'});
  await pg.waitForTimeout(500);
  await pg.evaluate(()=>{ window.__relayRx({type:'prefs',inactive:[],irons:[]});
    window.__relayRx({type:'machines',machines:[{id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:1,sessions:0,active:0}}]});
    handleMachineJson('clawd-atg',{type:'projects',projects:[
      {pid:'p1',name:'alpha',repoUrl:'https://github.com/clawdbotatg/alpha',kind:'gh',status:'ready',sessionCount:0,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:100,emoji:''},
      {pid:'p2',name:'bravo',repoUrl:'https://github.com/clawdbotatg/bravo',kind:'gh',status:'ready',sessionCount:0,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:90,emoji:''}]});
  });
  await pg.waitForTimeout(300);
  // land on alpha's sessions rung, where the "new session" composer lives
  await pg.evaluate(()=>{ location.hash = '#/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id); });
  await pg.waitForTimeout(400);
};
await bootPage(page);

const cdp = await page.context().newCDPSession(page);
const micXY = await page.evaluate(()=>{ const r=micBtn.getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2, visible:r.width>0&&r.height>0}; });
check('mic button visible on the sessions rung', micXY.visible, JSON.stringify(micXY));
check('#micBtn owns its touches (touch-action:none)',
  await page.evaluate(()=>getComputedStyle(micBtn).touchAction==='none'));
const holdMic = async ()=>{ await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:micXY.x,y:micXY.y}]}); await page.waitForTimeout(120); };
const releaseMic = async ()=>{ await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]}); await page.waitForTimeout(120); };

// --- 1. hold → dictate → box fills, draft saved -----------------------------
await holdMic();
check('hold starts recognition', await page.evaluate(()=>recOn===true && window.__sr.startedCount>=1));
await page.evaluate(()=>window.__emit(['hello world'],'again'));
check('results (final + interim) land in the box', await page.evaluate(()=>box.value==='hello world again'));
check('dictated text is saved as the draft', await page.evaluate(()=>(localStorage.getItem(draftKey(activeDraftId))||'')==='hello world again'));

// --- 2. release; a trailing final still lands in the untouched box ----------
await releaseMic();
check('release stops recognition', await page.evaluate(()=>recOn===false));
await page.evaluate(()=>window.__emit(['again yes'],null));
check('trailing final completes the sentence', await page.evaluate(()=>box.value==='hello world again yes'));

// --- 3. the user types → no result may ever replace the box again -----------
await page.evaluate(()=>{ box.focus(); box.setSelectionRange(box.value.length, box.value.length); });
await page.keyboard.type(' TYPED');
await page.evaluate(()=>window.__emit(['sneaky late chunk'],null));
check('late result cannot clobber typed text', await page.evaluate(()=>box.value==='hello world again yes TYPED'));

// --- 4. stranded-on recognition self-stops when typing appears --------------
await holdMic();                                    // recording again, legitimately
await page.evaluate(()=>window.__emit(['more'],null));
check('held mic still dictates after typing round', await page.evaluate(()=>box.value.endsWith('TYPED more')));
await page.keyboard.type(' MID');                   // user types while rec is (stuck) on
await page.evaluate(()=>window.__emit(['flood of ambient speech'],null));
const stuck = await page.evaluate(()=>({v:box.value, on:recOn, cls:micBtn.classList.contains('rec')}));
check('typing beats a live recognition: box kept', stuck.v.endsWith('TYPED more MID'), stuck.v);
check('…and the stranded recognition self-stopped', !stuck.on && !stuck.cls, JSON.stringify(stuck));
await releaseMic();

// --- 5. switching composer context kills dictation (no cross-draft bleed) ---
await holdMic();
await page.evaluate(()=>window.__emit(['bleed one'],null));
check('mic writes into alpha before the switch', await page.evaluate(()=>box.value.endsWith('bleed one')));
await page.evaluate(()=>{ location.hash = '#/p/' + encodeURIComponent(projectRows().find(p=>p.name==='bravo').id); });
await page.waitForTimeout(400);
await page.evaluate(()=>window.__emit(['bleed two'],null));
const bled = await page.evaluate(()=>({v:box.value, on:recOn,
  alpha: localStorage.getItem(draftKey('new:p1'))||''}));
check("context switch: bravo's empty box stays empty", bled.v==='' && !bled.on, JSON.stringify({v:bled.v,on:bled.on}));
check("…and alpha's stashed draft never got 'bleed two'", bled.alpha.endsWith('bleed one'), bled.alpha);
await releaseMic();

// ---- desktop page: SPACE-HOLD push-to-talk ---------------------------------
// A fresh non-emulated page: fine pointer → isTouch=false, real key events via
// CDP (keyboard.down twice = held key with repeat, exactly what a hold sends).
const dpage = await browser.newPage();
await dpage.addInitScript(initStub);
dpage.on('pageerror',e=>errors.push('desktop: '+String(e)));
await bootPage(dpage);
check('desktop page is not touch (space-hold armed)', await dpage.evaluate(()=>!isTouch));
await dpage.evaluate(()=>{ box.focus(); });

// --- 6. a quick space tap is just a space -----------------------------------
await dpage.keyboard.type('hi');
await dpage.keyboard.press(' ');
await dpage.keyboard.type('there');
await dpage.waitForTimeout(500);   // outlive the hold threshold: the tap must never fire it
check('quick tap types a normal space, no recording',
  await dpage.evaluate(()=>box.value==='hi there' && !recOn && !window.__sr));

// --- 7. hold past threshold → spaces rolled back, dictation runs ------------
await dpage.keyboard.down(' ');            // inserts a space…
await dpage.waitForTimeout(120);
await dpage.keyboard.down(' ');            // …auto-repeat inserts another…
await dpage.waitForTimeout(450);           // …then the hold threshold passes
const held = await dpage.evaluate(()=>({v:box.value, on:recOn, cls:micBtn.classList.contains('rec')}));
check('hold starts recognition and lights the mic', held.on && held.cls, JSON.stringify(held));
check('space(s) typed during the wait were rolled back', held.v==='hi there', JSON.stringify(held.v));
await dpage.evaluate(()=>window.__emit(['space talk'],null));
check('dictation lands after the rolled-back hold',
  await dpage.evaluate(()=>box.value==='hi there space talk'));

// --- 8. auto-repeat spaces are eaten while dictating ------------------------
await dpage.keyboard.down(' ');
await dpage.waitForTimeout(80);
check('repeat spaces mid-dictation do not reach the box',
  await dpage.evaluate(()=>box.value==='hi there space talk'));

// --- 9. release stops; other key during the wait cancels the pending hold ---
await dpage.keyboard.up(' ');
await dpage.waitForTimeout(250);                  // outlives SPACE_REPEAT_GRACE_MS
check('space release stops recognition', await dpage.evaluate(()=>!recOn && !micBtn.classList.contains('rec')));
check('dictated text saved as the draft', await dpage.evaluate(()=>(localStorage.getItem(draftKey(activeDraftId))||'')==='hi there space talk'));
await dpage.keyboard.down(' ');            // start a hold…
await dpage.waitForTimeout(100);
await dpage.keyboard.press('x');           // …but type through it: cancels the pending hold
await dpage.keyboard.up(' ');
await dpage.waitForTimeout(500);
check('typing during the wait cancels the hold, space kept',
  await dpage.evaluate(()=>box.value==='hi there space talk x' && !recOn));

check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed ? 'FAIL' : 'PASS — dictation writes only into a box it owns; typing and navigation always win');
process.exit(failed ? 1 : 0);
