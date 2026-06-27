// Test harness: extracts the [e2e-core] region from ../index.html and runs the
// browser E2E crypto in Node (same WebCrypto) against vectors from Python, so we
// prove the JS and fleet/e2e.py agree byte-for-byte without a browser.
// Invoked by test_e2e_interop.py:  node _e2e_node_harness.js <vectors.json>
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const core = html.split('// [e2e-core-begin]')[1].split('// [e2e-core-end]')[0];

const b64uDec = (s) => { s = s.replace(/-/g, '+').replace(/_/g, '/'); s += '='.repeat((4 - s.length % 4) % 4); return new Uint8Array(Buffer.from(s, 'base64')); };
const b64uEnc = (buf) => Buffer.from(buf).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

const E2E = (new Function('b64uEnc', 'b64uDec', 'crypto', core + '\nreturn E2E;'))(b64uEnc, b64uDec, globalThis.crypto);

const hx = (h) => new Uint8Array(Buffer.from(h, 'hex'));
const xh = (u) => Buffer.from(u).toString('hex');

(async () => {
  const v = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const T = E2E._test;

  const ks = await T.keySchedule(hx(v.Z), hx(v.mid), hx(v.epk_m), hx(v.n_m), hx(v.epk_w), hx(v.n_w), hx(v.ik_w));
  const ksOut = {};
  for (const k of ['Th', 'k_m2w', 'k_w2m', 'iv_m2w', 'iv_w2m', 'kc_m', 'kc_w', 'challenge', 'resume_master', 'resume_id']) ksOut[k] = xh(ks[k]);

  // resume keys from a fixed master+rn (label-mismatch guard)
  const rk = await T.resumeKeyset(hx(v.resume_master), hx(v.resume_rn));
  const rkOut = {}; for (const k of ['k_m2w', 'k_w2m', 'iv_m2w', 'iv_w2m']) rkOut[k] = xh(rk[k]);

  const keys = { k_m2w: hx(v.keys.k_m2w), k_w2m: hx(v.keys.k_w2m), iv_m2w: hx(v.keys.iv_m2w), iv_w2m: hx(v.keys.iv_w2m) };
  const sess = await new T.Session(keys).init();
  const opened = await sess.open(hx(v.py_w2m_record));          // open a Python worker→mobile record
  const sealed = await sess.seal(E2E.KIND_JSON, new TextEncoder().encode(v.js_seal_plain));  // seal mobile→worker, seq 0

  const sigOk = await T.verifySig(hx(v.ik_w_sig), hx(v.sig), hx(v.sig_msg));

  process.stdout.write(JSON.stringify({
    ks: ksOut,
    rk: rkOut,
    opened: { kind: opened.kind, payload: Buffer.from(opened.payload).toString('utf8') },
    sealed: xh(sealed),
    sigOk,
  }));
})().catch((e) => { process.stderr.write('NODEERR ' + (e.stack || e)); process.exit(1); });
