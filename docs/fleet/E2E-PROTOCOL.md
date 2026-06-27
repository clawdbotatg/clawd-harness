# Fleet end-to-end channel — protocol spec `fleet-e2e/1`

**Status:** normative. The Python worker (`fleet/e2e.py`), the browser
(`index.html`), and the relay passthrough MUST agree on every byte described
here. Change the version string if you change the wire format.

## 1. Goal & threat model

The relay is **public and untrusted**. TLS terminates at the relay's nginx, so
the relay process sees plaintext of everything it routes. We want a channel
between the **mobile** (a browser holding a WebAuthn passkey) and the **worker**
(a laptop running a real Claude harness) such that:

- **Confidentiality + integrity** of all session traffic is end-to-end between
  mobile and worker. The relay only ever sees ciphertext + routing metadata.
- **Mutual authentication:** the mobile proves it is talking to the *real*
  worker (not the relay); the worker proves a *human* authorized this exact
  channel with a hardware passkey.
- **No session hijack:** a malicious relay that observes/reorders/drops/injects
  frames cannot read traffic, forge authorization, or ride an established
  session. Relay compromise degrades to **denial of service only**.
- **Forward secrecy:** compromise of long-term keys later does not decrypt past
  sessions.
- **Replay/reorder resistance** within and across sessions.

Trusted: the mobile browser (+ its passkey in the Secure Enclave / iCloud
Keychain) and the worker (laptop). Untrusted: the relay and the network.

### Out of scope / residual trust
- **First-contact identity pinning (TOFU).** The mobile pins the worker's
  long-term public key on first use. A relay that is malicious *on the very
  first connection to a machine* could present a fake key. Mitigation: the
  worker prints a short **fingerprint** at enrollment; the UI shows the pinned
  fingerprint; the user verifies it once out-of-band and is warned loudly if it
  ever changes. After the first pin, the relay can never substitute the worker.
- **Traffic analysis.** The relay sees which mobile talks to which machine, and
  message sizes/timing. Contents are encrypted.
- **Endpoint compromise.** If the laptop or the unlocked phone is compromised,
  this protocol does not help — that is the trust root.

## 2. Cryptographic primitives

All on the **NIST P-256** curve to match WebCrypto (native in browsers) and the
existing `webauthn.py` verifier.

| Use | Primitive | Browser | Worker |
|-----|-----------|---------|--------|
| Worker long-term identity | ECDSA P-256 / SHA-256 | verify (WebCrypto) | sign (`cryptography`) |
| Ephemeral key agreement | ECDH P-256 | WebCrypto | `cryptography` |
| Passkey (human factor) | WebAuthn (ES256) | navigator.credentials.get | verify (`webauthn.py`) |
| Key derivation | HKDF-SHA256 | WebCrypto | `cryptography` |
| Record encryption | AES-256-GCM (128-bit tag) | WebCrypto | `cryptography` |
| Hashing / transcript | SHA-256 | WebCrypto | `hashlib` |

The **relay uses no new crypto** — it stays pure stdlib. It MAY re-verify the
passkey assertion it forwards (§6) using `webauthn.py`, but is never
authoritative.

### Encodings (must match exactly)
- **EC public keys:** raw uncompressed point `0x04 ‖ X(32) ‖ Y(32)` = 65 bytes.
  WebCrypto `exportKey("raw")` and `cryptography`
  `EllipticCurvePublicKey.from_encoded_point` both use this. JSON carries them
  base64url (no padding).
- **ECDH output:** the 32-byte big-endian X coordinate of the shared point
  (WebCrypto `deriveBits` and `cryptography` `exchange(ECDH())` agree).
- **ECDSA signatures on the wire:** raw `r(32) ‖ s(32)` = 64 bytes (WebCrypto
  format). The worker converts `cryptography`'s DER output to raw before
  sending; helpers live in `e2e.py`.
- **Lengths/sequence:** unsigned big-endian. Sequence numbers are 8 bytes.
- **base64url** everywhere in JSON, no `=` padding.

### Length-prefixed concatenation `LP(...)`
To make transcripts unambiguous, every variable field is encoded as
`uint16-BE(len) ‖ bytes`. `LP(a, b, c) = lp(a) ‖ lp(b) ‖ lp(c)`. Fixed-length
fields (nonces, hashes) are still wrapped for uniformity.

## 3. Identities & enrollment

- **Worker identity key `IK_w`:** an ECDSA P-256 keypair generated once on first
  run, persisted at `fleet/.fleet.worker_id.json` (private key PKCS#8,
  `chmod 600`, gitignored). Its public key `IK_w.pub` is the worker's identity.
  Fingerprint = `base32(SHA-256(IK_w.pub))[:20]` shown as 4×5 groups.
- **Passkey credentials:** WebAuthn public keys provisioned **out-of-band by an
  admin** (see DEPLOY) — there is **no web enrollment endpoint**, nothing
  network-reachable can add a credential. Stored at
  `fleet/.clawd-fleet.passkeys.json` on the worker (**authoritative** — the worker
  trusts only this local file, never the relay) and copied to the relay (edge
  doorman). The user credential is the passkey alone (no mobile token).
- **Pinning:** the mobile stores `IK_w.pub` per machine id in localStorage on
  first contact; mismatch on a later connect is a hard, loud failure.

The mobile receives `IK_w.pub` (and the fingerprint) in the relay roster. It is
*pinned*, not *trusted from the relay each time*: after first pin, a substituted
key is rejected.

## 4. Handshake

Four messages, routed by the relay by id (the relay forwards them blindly; it
MAY inspect/verify but cannot tamper undetected). `M` = mobile, `W` = worker,
`mid` = machine id, `PROTO = "fleet-e2e/1"`.

```
M  ──(1) ClientHello ──▶  R  ──▶  W
M  ◀──(2) ServerHello ──  R  ◀──  W
M  ──(3) ClientAuth  ──▶  R  ──▶  W      (R MAY verify the passkey here)
M  ◀──(4) ServerDone ──  R  ◀──  W
                 channel open
```

**(1) ClientHello  M→W**
- `epk_m` — M ephemeral ECDH public key (65B)
- `n_m` — 32 random bytes
- `proto` = `PROTO`

**(2) ServerHello  W→M**
- `epk_w` — W ephemeral ECDH public key (65B)
- `n_w` — 32 random bytes
- `ik_w` — W identity public key (65B), so M can check its pin
- `sig_w` — ECDSA over `T1` by `IK_w` (raw r‖s), where
  `T1 = SHA-256( LP(PROTO, mid, epk_m, n_m, epk_w, n_w, ik_w) )`

On (2), **M verifies `sig_w` against its pinned `IK_w.pub`** (and that `ik_w`
equals the pin). If either fails → abort (a relay tried to swap the worker or
its ephemeral).

**Both sides now derive the key schedule (§5).** The derived
`webauthn_challenge` is what the passkey signs.

**(3) ClientAuth  M→W**
- `assertion` — WebAuthn assertion object: `{credentialId, authenticatorData,
  clientDataJSON, signature}` (all base64url). Produced by
  `navigator.credentials.get` with `challenge = webauthn_challenge` (§5) and a
  fresh user-verification gesture (Face ID / Touch ID).
- `cf_m` — `HMAC-SHA256(kc_m, "fleet-e2e/1 client-finished")` (key confirmation)

W verifies, in order, aborting on any failure:
1. `cf_m` matches → M derived the *same* keys (so no relay-spliced ephemeral).
2. WebAuthn assertion via `webauthn.py`:
   - signature valid for an **enrolled** credential public key;
   - `rpIdHash == SHA-256("h.atg.link")`;
   - flags: **UP** (user present) **and UV** (user verified) set;
   - sign-count strictly greater than stored, unless both are 0 (Apple platform
     authenticators keep 0) — then accept and keep 0;
   - `clientDataJSON.type == "webauthn.get"`;
   - `clientDataJSON.origin == "https://h.atg.link"`;
   - **`clientDataJSON.challenge == base64url(webauthn_challenge)`** — this is the
     channel binding. The passkey signed *this* `epk_m`/`epk_w`/`n_*`/`ik_w`.
3. Anti-replay: `webauthn_challenge` not seen before in the worker's recent
   window (it is unique per handshake because it commits to fresh ephemerals).

**(4) ServerDone  W→M**
- `cf_w` — `HMAC-SHA256(kc_w, "fleet-e2e/1 server-finished")`

M verifies `cf_w`. On success the channel is **open**; both discard the
ephemeral private keys (forward secrecy) and keep only the derived record keys
and the session deadline.

### Why a malicious relay cannot win
- **Impersonate W to M:** needs `IK_w` private key to forge `sig_w` over a
  transcript containing the relay's own `epk`. It does not have it. M aborts.
- **Impersonate M to W:** needs to forge a WebAuthn assertion whose challenge
  equals `H(transcript)`. It cannot (no passkey private key; bound to `epk_m`).
- **MITM with its own ephemerals:** the two sides then compute different
  transcripts → different keys → `cf_m`/`cf_w` mismatch, and the passkey
  challenge W expects ≠ the one M signed → abort.
- **Read traffic:** needs the ECDH shared secret from two ephemeral *private*
  keys it never sees (CDH hardness). It only ever has ciphertext.
- **Replay an old handshake:** ephemerals + nonces are fresh; the passkey
  challenge is therefore unique; sign-count is monotonic; W rejects repeats.

## 5. Key schedule

Let `Z = ECDH(epk_self_priv, epk_peer_pub)` (32-byte X coordinate).

```
salt = SHA-256( LP(n_m, n_w) )
Th   = SHA-256( LP(PROTO, mid, epk_m, n_m, epk_w, n_w, ik_w) )   # full transcript
PRK  = HKDF-Extract(salt, Z)                                     # HKDF-SHA256

# 32-byte outputs via HKDF-Expand(PRK, info, 32):
k_m2w     = Expand( "fleet-e2e/1 key m2w" ‖ Th )   # mobile → worker AEAD key
k_w2m     = Expand( "fleet-e2e/1 key w2m" ‖ Th )   # worker → mobile AEAD key
iv_m2w    = Expand( "fleet-e2e/1 iv m2w" ‖ Th )[:4]   # 4-byte GCM nonce salt
iv_w2m    = Expand( "fleet-e2e/1 iv w2m" ‖ Th )[:4]
kc_m      = Expand( "fleet-e2e/1 confirm-m" ‖ Th )  # M's key-confirmation key
kc_w      = Expand( "fleet-e2e/1 confirm-w" ‖ Th )
webauthn_challenge = SHA-256( "fleet-e2e/1 webauthn-challenge" ‖ Th )   # 32B

(All HKDF info labels use single ASCII spaces exactly as written; `Expand` is
HKDF-Expand(PRK, info, 32). These byte strings MUST be identical in the Python
and JS implementations.)
```

`Th` is mixed into *every* expand so all keys are bound to the full transcript
(identities, ephemerals, nonces). Compromise of `Z` alone is insufficient
without the transcript; distinct sessions never share keys.

## 6. Record layer (post-handshake)

Every application frame — whether a JSON harness control frame or a binary PTY
frame — is wrapped once:

```
inner      = kind(1 byte) ‖ payload         # kind 0x01 = JSON(UTF-8), 0x02 = binary PTY
seq        = per-direction uint64, starts at 0, +1 per frame, never reused
nonce      = iv_dir(4) ‖ seq(8)             # 12-byte GCM nonce, unique per (key,seq)
aad        = dir(1: 0x4D 'M'→W or 0x57 'W'→M) ‖ seq(8)
ciphertext = AES-256-GCM( k_dir, nonce, aad, inner )   # includes 16-byte tag
record     = seq(8) ‖ ciphertext
```

The `record` bytes are the opaque payload the relay routes by id (it never sees
`inner`). The receiver:
- rejects `seq` ≤ the highest accepted for that direction (strictly increasing →
  drops replays and reorder; a gap is allowed only forward, never backward);
- reconstructs the nonce, verifies the GCM tag with the matching `aad`;
- on any failure (bad tag, replayed/reordered seq, short record): **drop the
  frame and continue** — never advance `seq_recv` on a failed frame, and never
  signal *why* it failed (no error oracle: "bad tag" and "bad seq" are
  indistinguishable to the sender). Drop-and-continue rather than teardown is
  deliberate: tearing the session down on a single injected frame would hand a
  malicious relay a trivial one-frame kill of every session. Implementations MAY
  tear down after a threshold of consecutive failures to bound garbage injection.

`dir` in the AAD prevents reflection (a frame can't be replayed back the other
way). `kind` is inside the authenticated plaintext, so the relay can't change a
control frame into a PTY frame or vice-versa.

### Transport mapping
Handshake messages (1–4) ride dedicated control types the relay already forwards
by id without inspecting (`toMachine`/`reply`-style envelopes). Post-handshake
`record`s are carried on the existing binary path (relay opcode `0x2`,
`[len][id][record]`) in both directions, plus a JSON fallback
`{type:"e2e", to|from, r:<base64url record>}` for environments without binary.
The relay logic is unchanged except for routing the new control types — see
`docs/WS-PROTOCOL.md`.

## 7. Session lifecycle

- A completed handshake mints a **session** = the record keys + a deadline. There
  is no separate bearer token; possession of the keys *is* the authorization,
  and the keys never leave the endpoints.
- **TTL:** slide-on-activity. `idle_deadline = now + IDLE_TTL` (default 10 min),
  refreshed on each *successfully authenticated* inbound record. A hard ceiling
  `hard_deadline = established + MAX_TTL` (default 60 min) is never extended.
- On expiry the worker zeroizes the keys and refuses further records; the mobile
  must run a fresh handshake (one new Face ID). The harness link for that viewer
  is torn down.
- The worker opens / forwards to its local harness **only** for a viewer with a
  live session. No session ⇒ the worker does nothing (the core requirement).

### 7.1 Resumption (reconnect without a fresh passkey)
A page reload is a new WebSocket → new relay mobile id → no worker session, which
would force a passkey on every load. Resumption avoids that **within the existing
TTL**:
- The key schedule (§5) also derives `resume_master = Expand("…resume-master" ‖ Th)`
  and `resume_id = Expand("…resume-id" ‖ Th)[:16]`. Both sides hold them; **neither
  crosses the wire**, so the relay can't derive resume keys.
- On a completed handshake the worker stores `resume_id → {resume_master,
  hard_deadline}`; the mobile stores `{resume_id, resume_master}` in localStorage
  with an expiry ≤ `MAX_TTL`.
- **Resume:** mobile → `{t:"e2e.resume", id}`. If the worker has a live, unexpired
  entry it picks a fresh nonce `rn`, derives **new** session keys
  `resume_keys(resume_master, rn)` (fresh keys ⇒ seq restarts at 0, no nonce
  reuse), re-attaches the session to the new mobile id, and replies
  `{t:"e2e.resumed", rn, cf}` where `cf = HMAC(resume_master, "…resume-confirm" ‖ rn)`.
  The mobile verifies `cf` (proving the worker holds the same master), derives the
  same keys, and the channel is live — **no passkey**.
- The resumed session inherits the **original `hard_deadline`** (resume never
  extends the 1 h ceiling); idle still slides. After the hard deadline the resume
  entry is dropped → next connect falls back to a full handshake (passkey).
- **Tradeoff:** `resume_master` is key material at rest (browser localStorage,
  worker memory) for ≤ `MAX_TTL` — the convenience/secrecy trade a TLS session
  ticket makes. A malicious relay still can't read it or derive keys (it only sees
  `resume_id`/`rn`); replaying a captured `resume_id` yields a session it has no
  keys for → DoS only, and forces the real mobile to re-auth.

## 8. Parameters

| Name | Default | Meaning |
|------|---------|---------|
| `FLEET_E2E_IDLE_TTL` | 600 s | idle timeout (slide) |
| `FLEET_E2E_MAX_TTL` | 3600 s | hard session ceiling |
| `FLEET_E2E_REQUIRE` | 1 | refuse un-E2E'd traffic (set 0 only for the stdlib smokes) |
| `FLEET_RP_ID` | h.atg.link | WebAuthn rpId checked by relay + worker |
| `FLEET_ORIGIN` | https://h.atg.link | WebAuthn origin checked by relay + worker |

## 9. Test vectors & required tests

`fleet/test_e2e.py` MUST cover: a full handshake producing identical keys on
both sides; a known-answer vector for the key schedule (fixed ephemerals/nonces
→ fixed `k_m2w/k_w2m/webauthn_challenge`); record seal/open round-trip;
rejection of replayed/reordered/tampered records; rejection of a tampered
`sig_w`, a wrong-challenge assertion, and a downgraded `proto`; idle + hard
expiry. `fleet/test_e2e_mitm.py` MUST simulate a malicious relay that tries to
substitute ephemerals / splice / replay and assert every attempt fails closed.

## 10. Versioning

`PROTO` appears in the transcript, every HKDF info string, and the WebAuthn
challenge. Any wire-format change MUST bump it (`fleet-e2e/2`), which
automatically invalidates cross-version handshakes (different keys, different
signatures) — fail-closed by construction.
