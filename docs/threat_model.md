# Threat model

What ephemera is defending against, what it deliberately is not, and where
the seams are. Read alongside [`SECURITY.md`](../SECURITY.md) (reporting
posture) and [`AGENTS.md`](../AGENTS.md) §5 (the failure-mode checklist
applied to every feature).

## Operating context

Single-admin, self-hosted one-time-secret service. Users are provisioned
explicitly by the operator via the admin CLI; there is no self-signup. The
product surface is:

- **Sender** — authenticated; web UI at `/send` (session cookie) or programmatic
  callers via `Authorization: Bearer eph_…` (DB-issued API token).
- **Receiver** — unauthenticated by design. Receives a URL of the form
  `/s/{token}#{client_half}` from a sender, opens it once, the secret is
  destroyed.
- **Operator** — has shell access on the host; runs the admin CLI; reads
  journald for the audit log; maintains the deploy.

The trust model assumes:

1. The operator is trusted. Shell access on the host implies full
   compromise — admin CLI, DB, env file, all reachable from there.
2. The sender is authenticated. Their identity is the accountable subject
   for every state-changing action they take.
3. The receiver is untrusted **and unidentified**. Anyone holding the URL
   can attempt the reveal; ephemera does not record who they are, by
   design (see "Receiver anonymity" below).
4. Network between client and Caddy is over TLS (Let's Encrypt). Caddy
   terminates and forwards plaintext to uvicorn over loopback.
5. The `EPHEMERA_SECRET_KEY` env var is operator-managed and confidential.

## In scope

- The FastAPI application under `app/`.
- The admin CLI (`python -m app.admin`).
- The auto-deploy workflow + the in-repo deploy script.
- Cryptographic surface (`app/crypto.py`, `app/auth/`).
- Authentication, rate-limiting, audit logging, telemetry (aggregate-only,
  opt-in).

## Out of scope

- Operator misconfiguration. Examples: setting
  `EPHEMERA_E2E_TEST_HOOKS=1` in production (deliberate test-only
  surface), pointing `EPHEMERA_DB_PATH` at a world-readable file,
  deploying without TLS, leaving the deploy SSH key on a shared CI
  runner.
- The `audit/` directory at the repo root (private, gitignored, not
  shipped, no surface to attack).
- Compromise of the operator's host. If the operator's machine or shell
  is compromised, every credential in the running system is reachable
  by definition.
- Compromise of an end user's device. Browser malware, keyloggers,
  shoulder-surfing, etc. are between the user and their device.
- Upstream library vulnerabilities. Reported to the library directly;
  our handling of a fixed upstream version is in scope once the CVE is
  published and a pinned bump is available (see Dependabot config).

## What ephemera defends against

### Database breach

A read of the SQLite DB alone cannot decrypt any secret. The Fernet key
that encrypts each secret is split into two 16-byte halves: `server_key`
is stored in the row, `client_half` is placed in the URL fragment (`#…`).
Per RFC 3986 the fragment never reaches the server, so the operator's
own DB + their server logs combined still don't carry both halves.

- DB dump → only `server_key` and ciphertext. No fragment, no plaintext.
- Server access logs → URL paths recorded (see "Token-in-path leak"
  below) but fragments stripped by every standard HTTP server.
- Audit log → no key material, ever (`security_log.emit`'s field-shape
  conventions reject plaintext-equivalent fields at the call site).

### Authentication attacks

- **Brute force**: bcrypt cost 12 + 10/min/IP login limiter + per-user
  lockout (10 failed attempts → 1h lock; counter is monotonic with no
  decay window so a slow-burn attacker cannot sit just below the
  threshold indefinitely).
- **Username enumeration**: constant-time bcrypt check even on
  unknown-user paths. The wire returns identical "invalid credentials"
  on every failure shape (wrong username / wrong password / wrong
  TOTP / wrong recovery code). The audit log internally distinguishes
  the reasons for triage; operators see the per-factor signal,
  attackers see the same 401.
- **TOTP replay**: every accepted step is recorded in `users.totp_last_step`
  and rejected on subsequent submissions. ±1 step tolerance is kept
  for clock-skew tolerance but doesn't widen the replay window.
- **Recovery-code DoS**: TOTP failures bump `totp_last_step` even on
  the failure path, but recovery-code failures do NOT consume the code
  — the attacker can't drain a victim's rescue pool by triggering
  failed logins.
- **Session fixation**: session value is rotated on every successful
  login; cookie carries `(user_id, session_generation)` so a credential
  rotation invalidates every live session in a single counter bump.

### CSRF / cross-origin state change

`Origin` header is validated on every state-changing route against the
configured allow-list. Cross-origin returns 403. Missing-Origin is
allowed only for callers presenting a *valid* bearer token (CLI / curl
flow — no ambient credentials, so no CSRF risk; the gate validates the
token via `lookup_api_token` before honoring the carve-out, so a
garbage `Bearer xxx` doesn't bypass).

`SameSite=Strict` on the session cookie is the primary CSRF defense for
browser callers; the Origin check is the explicit second layer.

### Reveal abuse

- **Brute-forcing a passphrase**: secret burns after 5 wrong attempts
  (configurable via `EPHEMERA_MAX_PASSPHRASE_ATTEMPTS`); the row is
  hard-deleted, the URL stops working.
- **Reveal storm against random tokens**: 10/min/IP reveal limiter.
- **Bogus-token probing**: `/s/{token}/meta` rides the 300/min/IP
  generic read limiter so the existence-oracle endpoint can't be
  hammered cheaply.

### Resource exhaustion

- Form-field length caps above the rate limits as a second defense
  (username/password/code = 256/256/64 chars at `/send/login`;
  passphrase/label = 200/60 at `/api/secrets`).
- Image upload capped at 10 MB at the app + 11 MB at Caddy
  (`request_body max_size`). MIME validation by magic bytes, not the
  Content-Type header. SVG explicitly rejected (XSS vector).
- Background cleanup task runs every 60 s (configurable via
  `EPHEMERA_CLEANUP_INTERVAL_SECONDS`) and purges expired rows + 30-day-
  old tracked metadata + aged-out limiter buckets.

### TOTP at-rest exposure

`users.totp_secret` is encrypted at rest under a KEK derived via
HKDF-SHA256 from `EPHEMERA_SECRET_KEY`. Backup tarballs, raw-SQL queries,
and casual DB inspection see only `v1:`-prefixed Fernet ciphertext.
Cost: rotating `EPHEMERA_SECRET_KEY` bricks every stored TOTP — the
recovery is for each user to present a recovery code and run
`rotate-totp`.

### Unprivileged service runtime

systemd sandbox: `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
`PrivateDevices`, `ProtectKernel*`, `NoNewPrivileges`. Service runs as an
unprivileged `ephemera` user with no shell. Env file at `0640
root:ephemera`.

## Known posture observations

These aren't defects — they're acknowledged properties of the design,
documented so an operator/reporter can reason about them.

### Token-in-path leak to access logs

`GET /s/{token}` and `POST /s/{token}/reveal` carry the token as a URL
path component. Caddy and uvicorn both record full paths in their access
logs by default. A leaked access log gives a reader "this IP probed this
token at this time."

The token alone cannot decrypt: the `client_half` lives in the URL
fragment (`#…`) which never crosses the wire as part of any request.
But the token *can* be used to call `/s/{token}/meta` and observe whether
the secret exists / requires a passphrase, and to attempt the reveal
(which fails without the fragment). So an access-log leak combined with
a URL-fragment leak is the join required for plaintext exposure — neither
half alone suffices.

Mitigations available to operators:

- Cap journald retention (see [`deployment.md`](deployment.md) §"Audit
  log retention").
- Caddy can be configured to log a redacted path for `/s/*`; the
  default config doesn't, but the option is open.
- Shorter token lifetimes (`expires_in`) reduce the window where a
  leaked token is still usable.

### Receiver anonymity

Receivers are unauthenticated by design. The audit log carries
`secret_id` on receiver-side events (`reveal.wrong_passphrase`,
`reveal.burned`) but **not** the receiver's IP; successful reveals do
not emit an audit event at all. The defender's signal — "this secret is
under attack" — is preserved by the same `secret_id` repeating across N
wrong-passphrase events; whether the attacker is behind one IP or
rotating doesn't change the response (the burn-after-N defense fires on
the secret_id, not on the IP).

This is asymmetric with sender-side events (which DO log user_id +
username + IP), and the asymmetry is deliberate: senders are
authenticated subjects accountable to their actions; receivers are not.

### Audit-log retention is operator-bounded

The Python process writes structured events to `journalctl -u ephemera`
via the `ephemera.security` logger. Retention is whatever the host's
journald is configured for — by default, "until disk pressure." Hard-
deleted secrets are gone, tracked metadata purges at 30 days, but the
audit log does not have an analogous retention story baked into the
application. Recommended: bound it via journald's `SystemMaxFileSec` or
`SystemMaxFiles` (see [`deployment.md`](deployment.md)).

### Aggregate analytics

`analytics_events` is an aggregate-only product-telemetry table.
Per-event-type payload schemas are pinned in `app/analytics.py::EVENT_REGISTRY`
with the `_PRESENCE_ONLY_INVARIANT` flag and a corresponding test in
`tests/test_analytics.py`. The privacy invariant (no `user_id`, no
payload that could fingerprint an individual under aggregation) is
gated by the operator-level `EPHEMERA_ANALYTICS_ENABLED` env *and* the
per-user `users.analytics_opt_in` consent column; if either is false,
the event-emit site no-ops.

### Active-operator code-swap (out of current scope)

The operator serves the JavaScript that the user's browser executes. An
actively malicious operator could in principle ship code that exfiltrates
plaintext before the URL-fragment split is constructed in the browser.
Closing this is the explicit motivation for the
[end-to-end encryption proposal](proposals/end-to-end-encryption.md);
until that lands, "trust your operator" is a load-bearing assumption.
This is documented honestly in `README.md`'s "Honest caveat" section.

## Reporting

See [`SECURITY.md`](../SECURITY.md) for the reporting channel
(GitHub Security Advisories primary, email fallback) and the synthetic-
credentials ask for proof-of-concepts.
