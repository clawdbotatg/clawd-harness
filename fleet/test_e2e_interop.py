#!/usr/bin/env python3
"""Interop: prove the browser E2E crypto (index.html [e2e-core]) and fleet/e2e.py
agree byte-for-byte — key schedule, AES-GCM records (both directions), ECDSA —
by running the JS in Node against vectors computed in Python. No browser needed.

Requires `node` on PATH (only for this test). Exits non-zero on any mismatch.
"""
import json
import os
import subprocess
import sys
import tempfile

import e2e

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    # Fixed, opaque inputs — the key schedule treats Z/pubkeys as bytes, so a KAT
    # exercises lp() / HKDF / labels / challenge without needing real EC points.
    Z     = bytes(range(32))
    mid   = b"austin-laptop"
    epk_m = b"\x04" + bytes((i * 3) & 0xFF for i in range(64))
    epk_w = b"\x04" + bytes((i * 5) & 0xFF for i in range(64))
    n_m   = bytes((i * 7) & 0xFF for i in range(32))
    n_w   = bytes((i * 11) & 0xFF for i in range(32))
    ik_w  = b"\x04" + bytes((i * 13) & 0xFF for i in range(64))

    ks = e2e.key_schedule(Z, mid, epk_m, n_m, epk_w, n_w, ik_w)

    # Python worker seals a worker→mobile record (seq 0); the JS mobile must open it.
    wsess = e2e.Session(ks, "worker")
    py_w2m_record = wsess.seal(e2e.KIND_JSON, b"hello from the worker")

    # Resume keys from a fixed master+rn — guards the rkey/riv label strings.
    resume_master = bytes((i * 17) & 0xFF for i in range(32))
    resume_rn = bytes((i * 19) & 0xFF for i in range(32))
    rk_py = e2e.resume_keys(resume_master, resume_rn)

    # ECDSA: Python signs, Node (WebCrypto) verifies.
    idp = e2e.gen_ec()
    ik_w_sig = e2e.pub_raw(idp.public_key())
    sig_msg = b"transcript-T1-stand-in"
    sig = e2e.sign_raw(idp, sig_msg)

    # Batched passkey: a set of transcript hashes (UNSORTED on purpose — both sides
    # must sort canonically before hashing, so this catches a sort/encoding drift).
    batch_ths = [bytes((i * 23 + off) & 0xFF for i in range(32)) for off in (40, 5, 200)]
    batch_ch_py = e2e.batch_challenge(batch_ths)

    vectors = {
        "Z": Z.hex(), "mid": mid.hex(), "epk_m": epk_m.hex(), "epk_w": epk_w.hex(),
        "n_m": n_m.hex(), "n_w": n_w.hex(), "ik_w": ik_w.hex(),
        "keys": {k: ks[k].hex() for k in ("k_m2w", "k_w2m", "iv_m2w", "iv_w2m")},
        "py_w2m_record": py_w2m_record.hex(),
        "js_seal_plain": "hello from the mobile",
        "ik_w_sig": ik_w_sig.hex(), "sig": sig.hex(), "sig_msg": sig_msg.hex(),
        "resume_master": resume_master.hex(), "resume_rn": resume_rn.hex(),
        "batch_ths": [t.hex() for t in batch_ths],
    }

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(vectors, f)
        vpath = f.name
    try:
        out = subprocess.run(["node", os.path.join(HERE, "_e2e_node_harness.js"), vpath],
                             capture_output=True, text=True)
    finally:
        os.unlink(vpath)
    if out.returncode != 0:
        print("node harness failed:\n", out.stderr)
        sys.exit(1)
    js = json.loads(out.stdout)

    fails = []

    # 1) key schedule must match exactly
    for k in ("Th", "k_m2w", "k_w2m", "iv_m2w", "iv_w2m", "kc_m", "kc_w",
              "webauthn_challenge", "resume_master", "resume_id"):
        jk = "challenge" if k == "webauthn_challenge" else k
        if js["ks"][jk] != ks[k].hex():
            fails.append(f"key schedule {k}: py={ks[k].hex()} js={js['ks'][jk]}")
    if not fails:
        print("  ✓ key schedule identical (Th, keys, ivs, confirm, challenge, resume)")

    # 1b) resume keys must match exactly (rkey/riv labels)
    if all(js["rk"][k] == rk_py[k].hex() for k in ("k_m2w", "k_w2m", "iv_m2w", "iv_w2m")):
        print("  ✓ resume keys identical (rkey/riv labels match)")
    else:
        fails.append(f"resume keys mismatch: py={ {k: rk_py[k].hex() for k in rk_py} } js={js['rk']}")

    # 2) JS opened the Python worker->mobile record
    if js["opened"]["kind"] != e2e.KIND_JSON or js["opened"]["payload"] != "hello from the worker":
        fails.append(f"JS open of py record: {js['opened']}")
    else:
        print("  ✓ JS mobile decrypts a Python worker record (w2m)")

    # 3) Python opens the JS-sealed mobile->worker record
    jrec = bytes.fromhex(js["sealed"])
    try:
        kind, pt = e2e.Session(ks, "worker").open(jrec)
        if kind != e2e.KIND_JSON or pt != b"hello from the mobile":
            fails.append(f"py open of js record: {kind} {pt!r}")
        else:
            print("  ✓ Python worker decrypts a JS mobile record (m2w)")
    except Exception as ex:
        fails.append(f"py open of js record raised: {ex}")

    # 4) ECDSA cross
    if not js["sigOk"]:
        fails.append("JS failed to verify a Python ECDSA signature")
    else:
        print("  ✓ JS (WebCrypto) verifies a Python ECDSA P-256 signature")

    # 5) batched passkey challenge — JS and Python sort+hash the set identically
    if js.get("batchCh") != batch_ch_py.hex():
        fails.append(f"batch_challenge: py={batch_ch_py.hex()} js={js.get('batchCh')}")
    else:
        print("  ✓ batched passkey challenge identical (sorted set commitment)")

    if fails:
        print("\nINTEROP FAILURES:")
        for f in fails:
            print("  ✗", f)
        sys.exit(1)
    print("\nPASSED: browser E2E crypto and fleet/e2e.py agree byte-for-byte")


if __name__ == "__main__":
    main()
