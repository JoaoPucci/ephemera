# Proposal: end-to-end encryption

**Status**: draft — seeking feedback
**Current system**: see [`ARCHITECTURE.md`](../../ARCHITECTURE.md)
**Discussion**: please open a GitHub issue or discussion thread with your thoughts. This is a direction, not a commitment — the shape may change based on what people raise.

---

## TL;DR

Today, ephemera encrypts your secret on the **server**. That means the plaintext passes through the server's memory during the create request, and the passphrase is a policy gate ("the server chooses not to decrypt") rather than a cryptographic barrier.

This proposal moves encryption into the **browser**. The server stores ciphertext it genuinely cannot read. The passphrase becomes part of the key derivation, not a separate check. The operator's ability to read your secrets drops from "trivial, undetectable, passive" to "requires actively tampering with the JavaScript, which is inspectable and detectable."

---

## Why

Ephemera's stated value is "one-time, ephemeral, destroyed on view." Every similar tool taken seriously (Bitwarden Send, PrivateBin, SSSS) delivers that promise with client-side encryption. Ephemera currently doesn't, and should.

### The honest gap today

- **Plaintext in transit**: when you click "Create Secret," the unencrypted content travels to the server over TLS, spends microseconds in request-handler memory while the server encrypts it, and is then stored as ciphertext. Nothing logs it today, but nothing *cryptographically* prevents the operator from logging it.
- **Passphrase is a policy gate**: the server bcrypt-compares a submitted passphrase against a stored hash, and *chooses* to release the plaintext if they match. An operator who wanted the content could skip the check or log the submitted passphrase. The passphrase is not part of the encryption key.
- **The pitch doesn't match the architecture**: a user who thinks about what a one-time secret tool promises will assume the operator can't read it. Today, technically, the operator can.

### What this proposal closes

After the change:
- The server never sees plaintext. Not in transit, not at rest, not during processing.
- The passphrase (when set) is mathematically required to decrypt — no policy check, no server cooperation can bypass it.
- "The operator cannot read your secrets" becomes architecturally true, not a commitment you have to trust.

### What this proposal does NOT close

This is important to name up front, because it's easy to oversell:

- **The operator still serves the JavaScript that does the encryption.** A malicious operator could modify `sender.js` to exfiltrate plaintext to a side channel *before* encrypting. The JS runs in your browser; you can inspect it; a community could audit it; but absent third-party-verified delivery, you are still trusting the operator not to ship hostile JS.

The shift isn't "no more trust required." It's "trust moved from a place you cannot inspect (the server) to a place you can (the browser)." That's a genuinely different threat model:

| | Server-side crypto (today) | Client-side crypto (proposed) |
|---|---|---|
| How operator exfiltrates | Log request body — one line | Modify served JS to exfil before encrypt |
| Visible to user | Never | Yes: DevTools, network tab, diff against published source |
| Detectable by third parties | No | Yes: browser extensions, integrity tooling, community review |
| Attacker effort | One line, passive, forever | Ongoing active attack that leaves evidence |

It's a meaningful upgrade, not a total solution. A v0.4+ direction might add subresource integrity, reproducible builds, or a pinned-CDN JS delivery that would narrow the remaining gap.

---

## What users will notice

### Mostly: nothing

The sender form looks the same. The receiver page looks the same. URLs look almost the same (longer fragment, same `#key` pattern).

### Two user-visible changes

1. **Creating a passphrase-protected secret gets a brief "Deriving encryption key…" spinner** while the browser runs argon2id against the passphrase. ~0.5–1.5 seconds depending on device. Revealing a passphrase-protected secret has the same pause. This is load-bearing — it's what makes offline brute-force attacks expensive.

2. **"This secret will self-destruct after 5 wrong passphrases" is going away.** Today the server counts failures and burns the row after 5. With client-side crypto, the server doesn't know whether an attempt succeeded — so there's no "burn after N." Instead, the passphrase itself becomes the barrier. Argon2id makes each guess slow (~1 second each), and the secret still auto-expires on its original TTL.

### One thing quietly removed

- **The plaintext-JSON API endpoint (`POST /api/secrets` with JSON body) goes away.** Today it's usable with `curl` via an API token. Under the new model, callers would need to implement the crypto themselves — a per-language library tax. Dropping the API surface is the pragmatic call for a "friends + self" tool. If a real use case for programmatic access appears, it can come back in a later proposal as an opt-in "I accept that the server sees plaintext" path.

---

## Architecture

### Creating a secret — WITHOUT passphrase

```
  [browser]                                                 [server]
      |
      |  content_key  = random 32 bytes
      |  iv           = random 12 bytes
      |  ciphertext   = AES-GCM(content_key, plaintext, iv)
      |  split content_key into server_half (16B) + client_half (16B)
      |
      |  POST /api/secrets                                    |
      |  body: { ciphertext, iv, server_half, metadata... }   |
      |--------------------------------------------------->   |
      |                                                       |  store row
      |  <-- { token, id, expires_at } -----------------------|
      |
      |  build URL locally:
      |    https://host/s/{token}#{base64url(client_half)}
      |
      |  show URL to user
```

The server holds `{ token, ciphertext, iv, server_half }`. It has half of a 256-bit key and the ciphertext, which is useless without the other half.

### Creating a secret — WITH passphrase

```
  [browser]                                                 [server]
      |
      |  kdf_salt     = random 16 bytes
      |  kek          = argon2id(passphrase, kdf_salt)     <-- slow
      |  content_key  = random 32 bytes
      |  iv           = random 12 bytes
      |  ciphertext   = AES-GCM(content_key, plaintext, iv)
      |  split content_key into server_half + client_half
      |  wrapped_server_half = AES-GCM(kek, server_half, iv2)
      |
      |  POST /api/secrets                                    |
      |  body: { ciphertext, iv, wrapped_server_half, iv2,    |
      |          kdf_salt, metadata... }                      |
      |--------------------------------------------------->   |
      |                                                       |  store row
      |  <-- { token, id, expires_at } -----------------------|
      |
      |  build URL locally (same as before):
      |    https://host/s/{token}#{base64url(client_half)}
```

Now the server holds an *encrypted* half-key. Unwrapping it requires the passphrase — nobody, including the operator, can do it without that secret.

### Revealing a secret

```
  [browser]                                                 [server]
      |
      |  GET /s/{token}                                       |
      |--------------------------------------------------->   |  static landing page
      |
      |  GET /s/{token}/meta                                  |
      |--------------------------------------------------->   |
      |  <-- { has_passphrase, kdf_salt? } -------------------|
      |
      |  if has_passphrase: prompt user, show spinner
      |
      |  POST /s/{token}/reveal                               |
      |  (no body -- just commits the reveal)                 |
      |--------------------------------------------------->   |
      |                                                       |  delete or wipe row
      |  <-- { ciphertext, iv, server_half_or_wrapped,        |
      |        iv2?, kdf_salt? } ----------------------------|
      |
      |  if has_passphrase:
      |    kek = argon2id(typed_passphrase, kdf_salt)     <-- slow
      |    server_half = AES-GCM_decrypt(kek, wrapped, iv2)  <-- fails if wrong passphrase
      |  content_key = server_half + client_half
      |  plaintext  = AES-GCM_decrypt(content_key, ciphertext, iv)
      |  render plaintext
```

Note that **the passphrase is never sent to the server**. Not on create, not on reveal. The server sees only opaque bytes throughout the entire lifecycle.

---

## Design decisions (and the reasoning)

### Cryptography

- **AES-GCM-256 for content encryption.** Native in WebCrypto, authenticated encryption (detects tampering), constant-time in every implementation that matters. Replaces Fernet (which is Python-specific and not available in the browser).
- **argon2id for key derivation from passphrase.** Memory-hard, GPU-resistant. Parameters `t=3, m=64MB, p=1` — about 1 second on a modern phone, the sweet spot between UX and brute-force resistance. Delivered via the [`hash-wasm`](https://github.com/Daninet/hash-wasm) library (~200KB, MIT-licensed, actively maintained).
- **PBKDF2 rejected** despite being native in WebCrypto. It's orders of magnitude weaker against GPU attackers at the same wall-clock budget. For a tool whose primary protection for a leaked URL is the passphrase, argon2 is worth the bundle size.
- **Fresh random IV per encryption** — AES-GCM is catastrophically broken if you reuse an IV with the same key. 96-bit IVs, generated by `crypto.getRandomValues`.
- **Key splitting stays the same idea** as today: 32-byte key → 16-byte halves. The server gets one half (possibly wrapped in a passphrase-derived key), the URL fragment carries the other. Key splitting ensures a pure DB leak still can't decrypt.

### API surface changes

Endpoints that change:
- `POST /api/secrets` — multipart body now carries ciphertext + iv + server_half (or wrapped) + kdf_salt (if passphrase). Server never sees plaintext or passphrase. The JSON-plaintext variant is dropped.
- `GET /s/{token}/meta` — adds `kdf_salt` to the response when the secret has a passphrase, so the browser can start the KDF while the user is still typing.
- `POST /s/{token}/reveal` — body is empty. Returns `{ ciphertext, iv, server_half_or_wrapped, iv2?, kdf_salt? }`. Destruction happens on this call, regardless of whether the browser's subsequent decrypt succeeds.

Unchanged:
- `GET /send`, `/send/login`, `/send/logout`, `/api/me`, `/api/secrets/tracked`, `/api/secrets/{id}/status`, `/api/secrets/{id}/cancel`, `DELETE /api/secrets/{id}`.
- Auth (password + TOTP + recovery codes), rate limiting, CSRF Origin check, session cookies.

Dropped:
- API tokens for `POST /api/secrets`. Without a plaintext API, there's no use case for bearer-token secret creation. Tokens for the read-side endpoints (tracked list, status) could stay if anyone uses them — open question below.

### Database schema

```sql
-- Current:
CREATE TABLE secrets (
    ...
    ciphertext   BLOB,
    server_key   BLOB,      -- the server_half
    passphrase   TEXT,      -- bcrypt hash
    attempts     INTEGER,   -- wrong-passphrase counter
    ...
);

-- Proposed:
CREATE TABLE secrets (
    ...
    ciphertext   BLOB,
    iv           BLOB NOT NULL,    -- AES-GCM nonce for ciphertext (new)
    server_half  BLOB,              -- raw 16B, or wrapped 16B if passphrase
    iv2          BLOB,              -- AES-GCM nonce for wrapping (null if no passphrase)
    kdf_salt     BLOB,              -- argon2 salt (null if no passphrase)
    -- REMOVED: passphrase       (no server-stored hash — never seen)
    -- REMOVED: attempts         (no server-enforced burn)
    ...
);
```

### What breaks at migration

Short answer: every secret in the DB.

Longer answer: server-encrypted rows (pre-v0.3) and client-encrypted rows (v0.3+) use incompatible crypto primitives. There's no way to migrate existing rows — the server would need plaintext to re-encrypt, but we explicitly refuse to expose plaintext ever again.

The migration is **a hard cutover**: all existing secrets are deleted at deploy time. This is acceptable because:
- Secrets are inherently ephemeral; the longest expiry option is 7 days.
- Anyone with a live secret can be told in advance ("migration on date X; create fresh secrets after if needed").
- For a tool at "friends + self" scale, the coordination cost is trivial.

---

## Threat model

### Stronger against

- **Passive operator snooping.** The operator cannot read plaintext from the server alone. This was the biggest honesty gap.
- **Database leaks / backups / forensic access.** Ciphertext + server_half (or wrapped server_half) are useless without the URL fragment and (if set) the passphrase.
- **Compromised server process.** An attacker who pops the Python app doesn't magically gain access to plaintexts — the plaintexts aren't there.
- **Passphrase-protected secrets against any attacker missing the passphrase.** Even an adversary with full DB + URL has to brute-force argon2id offline, which is expensive.

### Same as today

- **URL leak** (wrong recipient, shoulder-surfed, screenshotted, forwarded). If the passphrase is set, the leaker still needs to find it. If no passphrase, the URL alone grants one-shot view — same as today.
- **Sender or receiver device compromise.** If your machine is owned, the attacker sees plaintext regardless of server-side or client-side crypto. Out of scope for any crypto scheme.
- **TLS compromise.** Nation-state or CA-level attacks. Out of scope.

### Still possible (honest about residuals)

- **Operator ships malicious JS.** The biggest remaining trust assumption. Mitigations outside this proposal's scope but worth noting as a future direction: SRI-pinned JS served from a third party, reproducible builds users can verify against public tags, or a browser extension that hashes the served scripts.
- **Coordinated attacker with URL *and* passphrase.** No defense — they have what's needed. This is inherent to any "one-time link + second factor" design.
- **Forgotten passphrase.** The secret is cryptographically unrecoverable. This is a feature of the design, not a bug, but the UX needs to warn users clearly.

---

## Migration plan

Phased, with tests green at each phase boundary. Order matters because some phases leave the system in a non-shippable state — see "Don't ship partway" below.

### Phase 0 — design lock-in (before any code)

- Finalize KDF parameters (`t=3, m=64MB, p=1` unless the UX work shows otherwise on low-end devices).
- Pick argon2 delivery strategy — eager bundle vs. lazy-load on first passphrase interaction.
- Decide whether API tokens survive for read-side endpoints or die entirely.
- Decide the user-facing announcement for the hard cutover.

### Phase 1 — server-side refactor

- Delete `app/crypto.py` (or reduce to a no-op).
- Schema migration: rename `server_key` → `server_half`, add `iv`, `iv2`, `kdf_salt`, drop `passphrase`, drop `attempts`. Idempotent, runs from `init_db()`.
- `POST /api/secrets`: accept ciphertext + crypto material, store it blindly.
- `GET /s/{token}/meta`: return `kdf_salt` when applicable.
- `POST /s/{token}/reveal`: return crypto material instead of plaintext.
- Drop the JSON-plaintext `POST /api/secrets` path.
- Pytest suite: heavy rewrite of `test_sender.py` and `test_receiver.py`. Gut `test_crypto.py`.

### Phase 2 — browser crypto module

- New file `app/static/crypto.js` with: key generation, splitting, AES-GCM wrap/unwrap, argon2id via hash-wasm, base64url helpers.
- Vitest unit tests for the module: roundtrip, wrong passphrase fails, tampered ciphertext fails, IV-reuse prevention.
- Bundle argon2 WASM into the static directory.

### Phase 3 — wire it into sender and reveal flows

- Rewrite `sender.js`'s submit handler to encrypt before POST.
- Rewrite `reveal.js` to decrypt after receiving crypto material.
- Add spinner UI + copy for the KDF wait.
- Update in-flight guards (the existing double-tap protection still applies).

### Phase 4 — test rewrite

- Vitest: updated `sender.test.js` and `reveal.test.js` using real WebCrypto in jsdom or carefully-stubbed crypto.
- Playwright: the golden-path smoke test now exercises real crypto end-to-end — a significant upgrade in coverage.

### Phase 5 — migration, docs, deploy

- Add the "wipe all secrets" step to the startup migration.
- Update `ARCHITECTURE.md` top to bottom.
- Update `README.md` passphrase section.
- Update `DEPLOYMENT.md` with a migration callout.
- Tag the release, deploy with the hard-cutover announcement.

### Don't ship partway

Phases 1–4 individually leave the system in a broken state (either the browser can't encrypt, or the server can't decrypt, or tests don't pass). Only after phase 5 is the system shippable again. Migration is "finish or don't start."

### Effort estimate

Rough: four to five focused evenings end-to-end. The first-version server plus v0.2's test infrastructure + mobile polish was about that much work.

---

## Trade-offs and what you give up

Naming these explicitly so nobody is surprised:

### UX

- **KDF latency**: ~1 second spinner on both create and reveal for passphrase-protected secrets. Worst on older phones.
- **No "5 wrong passphrases = burn" feedback**: gone by design. Offline attackers have infinite attempts (but each is expensive).
- **Error messages get vaguer**: the browser can distinguish "wrong passphrase" from "tampered ciphertext" only by how decryption fails, and telling the user which is which can leak information. Expect a single "could not decrypt — wrong passphrase or the secret has been modified" message.

### Operational

- **Server-side visibility on reveals shrinks to zero**: if someone reports "the secret didn't work," the server logs show only that a reveal was requested. All debugging moves to the user's browser console.
- **No server-side content features ever**: no preview thumbnails, no "this looks like a password, here's a tip," no content-aware expiry. Architectural commitment to opacity.
- **No emergency recovery**: there is no "I forgot the passphrase, can you unlock it for me." The answer is permanently "no, the system is designed so that I couldn't even if I wanted to."

### Development

- **Extra client-side dependency** (hash-wasm, ~200KB). Bundle size grows.
- **Two halves of the codebase now both need crypto-aware tests** (Python + JS), not just one.
- **Secure-context requirement**: `crypto.subtle` only works over HTTPS or on localhost. Plain-HTTP LAN dev stops working. Mitigated by `adb reverse` or tunneling for on-device testing.

---

## Open questions (feedback wanted)

1. **Is the UX cost of the KDF wait worth it?** Specifically on older phones. If the consensus is "yes," we keep argon2id with strong parameters. If the consensus is "it's too slow in practice," we step down to weaker parameters or consider the hybrid design (argon2 + server-side rate limit).
2. **Should API tokens survive for the read-side endpoints?** If nobody is using them, dropping the whole feature simplifies the codebase. If someone has a script polling `/api/secrets/tracked`, it stays.
3. **Should the hard cutover include an announcement window**, or is "the release notes said it would happen" sufficient given the user base?
4. **Do we want to invest in served-JS integrity** (SRI, third-party CDN, reproducible builds) as part of this proposal, or punt to a follow-up? The residual trust gap it closes is real but substantial additional work.
5. **Is "no burn after N wrong attempts" acceptable?** It's a real loss of a small safety feature. Argon2 makes each wrong attempt expensive, but the secret is no longer *destroyed* by repeated guesses.

---

## Prior art

Systems that look similar in spirit or shape:
- **Bitwarden Send** — commercial, same general model: client-side crypto, passphrase as key derivation, one-time links. Reference implementation for the UX questions.
- **PrivateBin** — self-hosted pastebin with client-side AES-GCM. Similar threat model to ephemera.
- **SSSS (Simple Secure Secret Sharing)** — same shape, minimal implementation.
- **Signal / iMessage** — solve end-to-end for streaming chat; the "shared link one-shot" model is subtly different but the crypto primitives are the same.

Ephemera is not novel here — this is the well-trodden path for tools of this category. v0.3 brings ephemera onto that path.

---

## How to give feedback

Open a GitHub issue. Labels:
- `proposal-feedback` — anything: a reaction, a concern, a suggestion, a question.
- `design-bug` — if you think a specific technical choice is wrong.
- `ux-concern` — if the KDF-wait, missing burn-after-N, or spinner UI looks bad to you.

All feedback before implementation starts is especially valuable because the direction is easier to change now than later. Once the code is shipped the conversation shifts from "should we?" to "should we revert?"

---

## Changelog

- **2026-04-18** — initial draft. Seeking feedback before Phase 0 decisions are locked in.
