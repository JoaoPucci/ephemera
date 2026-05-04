# Backend Architecture

The server side of ephemera: the Python app, the SQLite schema, the crypto
design, the security hardening, and the HTTP surface. Product-level
requirements are in [`requirements.md`](requirements.md); the browser side is
in [`frontend.md`](frontend.md); deployment details are in
[`deployment.md`](deployment.md).

## Tech stack

| Component        | Choice                      | Rationale                                      |
|------------------|-----------------------------|-------------------------------------------------|
| Framework        | FastAPI                     | ASGI-native, Pydantic validation, type hints    |
| ASGI server      | Uvicorn                     | Lightweight, purpose-built for ASGI             |
| Reverse proxy    | Caddy                       | Automatic HTTPS via Let's Encrypt, minimal config|
| Database         | SQLite                      | Zero-config, single-user sender, sufficient     |
| Encryption       | Fernet (`cryptography` lib) | Symmetric, authenticated, timestamped           |
| Passphrase hash  | bcrypt                      | Slow hash, resistant to brute-force             |
| Templating       | Jinja2 (i18n-aware)         | gettext per-request locale needs server-side render; see note below |
| Process mgmt     | systemd                     | Learning goal, Docker migration later           |
| Testing          | pytest + httpx + Playwright | httpx for FastAPI TestClient; Playwright for the acceptance suite |

**Note on Jinja and the locale story**: Page chrome is rendered server-side
through Jinja so every label, button, and aria-string is wrapped in a gettext
call resolved against the request's locale. The locale is decided per request
by middleware (`app/i18n.py::locale_middleware` -> cookie / Accept-Language
/ user `preferred_language`) and injected into every template via the
`template_context()` helper. Templates live in `app/templates/`; the
`jinja2.ext.i18n` extension is what binds `{{ _("...") }}` to the request's
translations object.

What the server still does *not* render server-side is the **payload** of any
secret. The revealed plaintext (text or image) is returned as JSON from
`POST /s/{token}/reveal` and painted client-side by `reveal.js`; that keeps
plaintext out of any rendered HTML, off the wire as a server-rendered
attribute, and -- by extension -- out of any HTML-shaped log line. Server
templating is only used for surfaces the server already owns (chrome,
landing-page shell, login form).

**Note on async and SQLite**: SQLite is inherently synchronous. Route handlers
that only perform DB work are defined as regular `def` (not `async def`) -- FastAPI
automatically runs these in a threadpool, which is the correct pattern. No need
for `aiosqlite` or `run_in_executor` boilerplate.

## Core flow

### Creating a secret (sender)

```
Sender                              Server
  |                                    |
  |-- GET /send ---------------------->|  (login with API key if no session)
  |<-- render form --------------------|
  |                                    |
  |-- POST /api/secrets -------------->|  (payload, expiry, optional passphrase,
  |   (multipart for images,           |   optional track flag)
  |    JSON for text)                  |
  |                                    |-- generate token (lookup ID)
  |                                    |-- generate full Fernet key
  |                                    |-- encrypt payload with full key
  |                                    |-- split key: server_half + client_half
  |                                    |-- store: token, server_half, ciphertext,
  |                                    |          metadata, passphrase_hash
  |                                    |
  |<-- { url: /s/{token}#{client_half},|
  |       id: <secret_id>,             |
  |       expires_at: ISO8601 } -------|
```

### Viewing a secret (receiver)

```
Receiver                            Server
  |                                    |
  |-- GET /s/{token} ----------------->|  (fragment #{client_half} NOT sent to server)
  |<-- landing page -------------------|  (explanation + "Reveal" button)
  |                                    |  (JS reads fragment from URL)
  |                                    |
  |   [if passphrase required]         |
  |   [receiver enters passphrase]     |
  |                                    |
  |-- POST /s/{token}/reveal --------->|  (body: { key: {client_half},
  |                                    |           passphrase: <if set> })
  |                                    |-- lookup by token
  |                                    |-- verify passphrase if required
  |                                    |-- reconstruct full key from halves
  |                                    |-- decrypt ciphertext
  |                                    |-- DELETE row from DB
  |                                    |
  |<-- decrypted content (text/image) -|
```

### Checking status (sender, optional)

```
Sender                              Server
  |                                    |
  |-- GET /api/secrets/<id>/status --->|  (requires auth)
  |<-- { status: pending|viewed|expired|
  |       expires_at: ISO8601 } -------|
```

Only available if `track: true` was set at creation time. When tracking is
enabled, the row is not fully deleted on reveal -- metadata (status, timestamps)
is kept but ciphertext and key material are wiped. Tracked metadata is purged
after 30 days.

## Security design

### Key splitting (zero-knowledge encryption)

The core security property: a database breach alone cannot decrypt any secret.

1. **Creation**: A 32-byte Fernet key is generated. It is split into two 16-byte
   halves: `server_half` and `client_half`.
2. **Storage**: `server_half` is stored in the DB alongside the ciphertext.
   `client_half` is placed in the URL fragment (`#`).
3. **Landing page load**: The receiver requests `GET /s/{token}`. The fragment is
   NOT sent to the server (per RFC 3986). The server returns a static landing
   page. JavaScript on the page reads the fragment from `window.location.hash`.
4. **Reveal**: The receiver clicks "Reveal". JS sends `POST /s/{token}/reveal`
   with `client_half` in the request body. The server reconstructs the full key,
   decrypts, returns the plaintext, and deletes the row.

This means:
- Server logs never contain key material.
- A DB dump yields only half the key -- useless without the fragment.
- Network interception of the landing page request yields no key material.
- Only the reveal POST (over TLS) carries the client half.

### Passphrase (optional second factor)

When set by the sender:
- The passphrase is hashed with bcrypt and stored alongside the secret.
- The landing page shows a passphrase input field in addition to the reveal button.
- The reveal POST includes the passphrase; the server verifies it against the
  bcrypt hash before decrypting. Failed attempts do NOT delete the secret but are
  rate-limited (max 5 attempts, then the secret is burned).

### MIME validation

Uploaded images are validated:
- MIME type must be one of: `image/png`, `image/jpeg`, `image/gif`, `image/webp`.
- File magic bytes are checked (not just the Content-Type header).
- Max size: 10 MB.
- SVG is explicitly rejected (XSS vector).

### Hardening

- Rate limiting: in-memory per-IP and per-session sliding-window counters in
  `app/limiter.py`. No external dependency: at single-instance scale a
  process-local dict + lock is enough, and a fresh-on-restart counter is an
  acknowledged property rather than a bug. The four named limiters
  (`reveal_rate_limit`, `login_rate_limit`, `create_rate_limit`,
  `read_rate_limit`) are the only shapes the codebase composes; the
  `test_state_mutating_routes_all_carry_rate_limiter` fitness function
  rejects any state-mutating route that ships without one.
- CSRF: The reveal action is a JSON POST initiated by JS (not a form submit).
  The `Origin` header is validated server-side against the configured host. Since
  the client half of the key is required in the body (only available to JS running
  on the page), this acts as a natural CSRF barrier.
- Security headers on all responses (via FastAPI middleware):
  - `Content-Security-Policy:` deny-by-default with explicit allow-list for
    what ephemera actually loads. Shape:
    `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
    connect-src 'self'; font-src 'self'; manifest-src 'self'; frame-ancestors 'none';
    form-action 'self'; base-uri 'self'; object-src 'none'`.
    `data:` stays allowed on `img-src` because reveal.js renders images as
    `data:<mime>;base64,...` and the chevron SVG in `style.css` is also
    inlined as a `data:image/svg+xml` URL. Anything not on this list is
    rejected by the browser rather than by the server.
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: no-referrer`
  - `Cross-Origin-Opener-Policy: same-origin` — isolates the browsing context
    from any cross-origin opener (e.g., a malicious `window.open`er).
  - `Cross-Origin-Resource-Policy: same-origin` — stops other sites from
    embedding ephemera's responses (images, JSON).
  - `Permissions-Policy` — empty allow-list for every sensor/hardware API
    (camera, microphone, geolocation, payment, USB, accelerometer, gyroscope,
    magnetometer, interest-cohort). None are used; denying pre-empts a
    future regression that quietly adds one.
  - `Strict-Transport-Security: max-age=86400` (conservative first-rollout value;
    HSTS is sticky, so start small, then bump to `max-age=31536000; includeSubDomains; preload`
    once the cert has survived at least one Let's Encrypt renewal. Caddy does *not* add
    HSTS automatically -- only the app middleware sets it.)
- No secret content in logs: Uvicorn access log format configured to exclude
  request bodies. FastAPI exception handlers scrub sensitive data.
- **Structured security audit log** via the `ephemera.security` logger. Every
  security-relevant mutation emits one JSON line: `login.success`,
  `login.failure` (with reason), `login.lockout`, `reveal.success`,
  `reveal.wrong_passphrase`, `reveal.burned`, `secret.canceled`,
  `secret.cleared`, `apitoken.created`, `apitoken.revoked`, `user.added`,
  `user.removed`, `password.reset`, `totp.rotated`, `recovery.regenerated`.
  See `app/security_log.py` for field schemas. The emit-site filters
  sensitive material; never pass plaintext/passphrase/client_half/password/
  totp_code/server_key/ciphertext into a field. Under systemd the events
  land in `journalctl -u ephemera`; `-o cat | grep '"event":"login.failure"' | jq`
  is the usual triage filter.
- Secrets hard-deleted (not soft-deleted) on reveal. If tracking is enabled, only
  the status flag and timestamps survive.
- Expired secrets purged by background cleanup (runs every 60 seconds via FastAPI
  lifespan event + `asyncio.create_task` with a simple loop).

### Sender authentication

Two credential types coexist:

1. **Password + TOTP** for the web form at `/send`. Intended for interactive use.
2. **Named API tokens** for programmatic callers (CLI scripts, CI, future
   integrations). These are DB-issued and revocable.

#### First-time provisioning

Run the CLI once at install time:

```
python -m app.admin init
```

This prompts for a password, generates a random 32-char base32 TOTP secret,
and prints (a) a terminal-rendered QR to scan into any TOTP authenticator app
and (b) ten one-time recovery codes shown once and never again. A `users` row
is created with a bcrypt hash of the password, the TOTP secret, and bcrypt
hashes of the recovery codes. The CLI refuses to overwrite an existing user;
credential rotation is explicit via `reset-password` / `rotate-totp` /
`regen-recovery-codes`, each of which re-authenticates before proceeding.

**Password policy.** The CLI enforces `len >= 10` and then submits the first
5 hex chars of the password's SHA-1 to the Have I Been Pwned k-anonymity
range API. If the rest of the hash is in the returned list, the prompt
rejects the password with the breach count and re-prompts. The plaintext
never leaves the host. If the API is unreachable (offline / DNS blip) the
prompt prints a warning and accepts the password — fail-open so admin
ops don't stall on a network issue. No mixed-class requirement (NIST
800-63B §5.1.1.2).

#### Login flow

```
POST /send/login
  form: password=<str>, code=<6-digit TOTP or 12-char recovery code>

server:
  1. If lockout_until > now → 423 {error: "locked", until: ISO8601}
  2. bcrypt.checkpw(password, stored_hash)       -- constant-time
  3. If code is 6 digits → pyotp verify against secret with ±1-step tolerance;
     check step > totp_last_step (anti-replay); on success, bump totp_last_step.
     Else treat as recovery code → bcrypt-compare against each unused hash;
     on match, mark that entry used_at = now.
  4. If either check fails: failed_attempts++; return 401 "invalid credentials"
     (identical surface for wrong password vs wrong code vs wrong recovery code).
     If failed_attempts >= MAX_FAILURES (10): set lockout_until = now + 1h,
     reset counter.
  5. On success: reset failed_attempts=0, lockout_until=NULL. Issue a fresh
     random session value (rotation prevents session fixation) and set a
     signed cookie via `itsdangerous`.
```

Rate limits (in-memory sliding window, per client IP):
- `POST /send/login`: 10 / minute
- `POST /s/{token}/reveal`: 10 / minute
- Generic read limiter at 300 / minute: `GET /api/me`,
  `GET /api/secrets/tracked`, `GET /api/secrets/{sid}/status`,
  `GET /s/{token}/meta`. Also covers the small mutation endpoints whose
  cost shape is read-like rather than create-like:
  `PATCH /api/me/preferences` (analytics opt-in toggle),
  `PATCH /api/me/language` (locale preference), and `POST /send/logout`.
  Without it, `meta`-spam (and the equivalent on the prefs / language
  endpoints) is a cheap DoS vector.

Per-session rate limit (applies to authenticated callers):
- `POST /api/secrets` and the per-secret mutation routes
  (`POST /api/secrets/{id}/cancel`, `DELETE /api/secrets/{id}`,
  `POST /api/secrets/tracked/clear`): 60 / hour per session. Contains
  blast radius of a hijacked session.

All in-memory counters reset on process restart — an acknowledged limit
at personal-instance scale.

Form-field length caps sit above the rate limits as a second defense:
`/send/login` refuses requests with `username`/`password`/`code` longer
than 256/256/64 chars, and the multipart `/api/secrets` path caps
`passphrase` (200) and `label` (60). These prevent CPU waste on bcrypt
or multipart parsing for obvious junk if anything ever lets a large body
past Caddy.

Session cookies are `HttpOnly`, `SameSite=Strict`, `Secure` in production. A
logout endpoint (`POST /send/logout`) clears the cookie.

Each cookie is signed over `(user_id, session_generation)`. The user row
carries a `session_generation` counter that is bumped on every credential
rotation (`reset-password`, `rotate-totp`, `regen-recovery-codes`). A cookie
signed over the prior generation stops authenticating the moment the
counter advances, so a rotated-away password or a freshly-regenerated TOTP
also sign the user out of every live session without waiting for
`session_max_age` to elapse.

`Secure` is driven by `EPHEMERA_SESSION_COOKIE_SECURE` (default `true`). In
dev on `127.0.0.1`/`localhost` the cookie still works because modern browsers
treat loopback as a secure context.

The `SameSite=Strict` choice has a UX consequence: a click on `/send` from
outside the app (email, Slack, search engine result) lands logged-out in
that tab even if the user has a valid session, because Strict suppresses the
cookie on cross-origin top-level navigations. For an admin-only tool this
is the right trade — the protection against cross-origin state change is
unconditional. If ephemera ever opens to wider use, revisit whether
`SameSite=Lax` + an explicit CSRF token better fits the UX.

#### API tokens

External callers send `Authorization: Bearer <token>` where `<token>` was
minted with `python -m app.admin create-token <name>`. The server stores
`SHA-256(plaintext)` only; lookup is constant-time via a unique-indexed
hash column. Tokens are revocable by name (`revoke-token`) and listable
(`list-tokens`). A token is accepted only when `revoked_at IS NULL`; every
successful use updates `last_used_at`.

Tokens and web sessions are equivalent for `/api/secrets` and the status
endpoint (`Depends(verify_api_token_or_session)`). The web form doesn't mint
tokens; it rides the session cookie. This means:

- A leak of a `.env` file no longer yields credentials -- there is no shared
  secret in env anymore. The only credentials live in the SQLite DB (bcrypt
  hashes, TOTP secret, SHA-256 token digests).
- Compromise of one API token can be scoped to one purpose (e.g., "ci-runner")
  and revoked independently of the rest.

#### Origin enforcement

`Origin` header is validated on all state-changing routes (`POST /send/login`,
`POST /send/logout`, `POST /api/secrets`, `DELETE /api/secrets/{id}`,
`POST /api/secrets/{id}/cancel`, `POST /api/secrets/tracked/clear`,
`POST /s/{token}/reveal`). Requests from a foreign Origin get 403.

Missing Origin is allowed **only** for callers using a bearer token
(`Authorization: Bearer …`) — the CLI/curl flow. Bearer-token clients carry
no ambient credentials, so CSRF does not apply. Missing Origin on a
session-cookie request is refused (403) — that's the exact shape of the
CSRF gap we explicitly refuse. `SameSite=Strict` remains the primary defense;
the Origin check is a second layer.

## Database schema

Five tables: the three product tables (`users`, `secrets`, `api_tokens`),
the aggregate-only `analytics_events` table, and a one-row `schema_version`
table that anchors the migration registry. The canonical definitions live
in `app/models/_core.py::TABLES_SCRIPT`; the version below is the same SQL
with comments added inline.

```sql
CREATE TABLE users (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    username              TEXT NOT NULL CHECK (length(username) <= 256),
    email                 TEXT,                    -- nullable until email flows land
    password_hash         TEXT NOT NULL,           -- bcrypt, cost 12
    totp_secret           TEXT NOT NULL,           -- Fernet(HKDF(SECRET_KEY)) of the base32 seed; prefixed "v1:"
    totp_last_step        INTEGER NOT NULL DEFAULT 0,    -- anti-replay: reject step <= this
    recovery_code_hashes  TEXT NOT NULL DEFAULT '[]',    -- JSON: [{"hash": bcrypt, "used_at": ISO8601|null}]
    failed_attempts       INTEGER NOT NULL DEFAULT 0,
    lockout_until         TEXT,                    -- ISO8601 or NULL
    session_generation    INTEGER NOT NULL DEFAULT 0,    -- bumped on credential rotation; signed into every session cookie
    preferred_language    TEXT,                    -- BCP-47 tag (e.g. 'ja', 'pt-BR'); NULL falls back to cookie / Accept-Language / default
    analytics_opt_in      INTEGER NOT NULL DEFAULT 0
        CHECK (analytics_opt_in IN (0,1)),         -- 1 = user explicitly consented; "never saw the toggle" and "explicitly declined" are operationally identical (both mean "do not emit") and indistinguishable in the row by design
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_users_username ON users(username);
CREATE UNIQUE INDEX idx_users_email    ON users(email) WHERE email IS NOT NULL;

-- The CHECK clauses on user-controlled TEXT columns mirror the documented
-- Pydantic ceilings in app/schemas.py. Defense in depth: a future write
-- path that bypasses the Pydantic boundary still gets the row rejected at
-- the storage layer rather than persisting data above the application
-- contract. 80 chars on `passphrase` allows headroom over the 200-char
-- input limit because the column stores the bcrypt OUTPUT (~60 chars),
-- not the raw passphrase.
CREATE TABLE secrets (
    id            TEXT PRIMARY KEY,                -- UUID4 (sender status lookups)
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token         TEXT UNIQUE NOT NULL,            -- URL-safe random token (receiver URLs)
    server_key    BLOB,                            -- server half of the Fernet key (NULL after reveal if tracked)
    ciphertext    BLOB,                            -- encrypted payload (NULL after reveal if tracked)
    content_type  TEXT NOT NULL,                   -- 'text' or 'image'
    mime_type     TEXT,                            -- 'image/png', etc. NULL for text
    passphrase    TEXT CHECK (passphrase IS NULL OR length(passphrase) <= 80),
    track         INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'viewed' | 'burned' | 'canceled' | 'expired'
    attempts      INTEGER NOT NULL DEFAULT 0,      -- failed passphrase attempts
    label         TEXT CHECK (label IS NULL OR length(label) <= 60),
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    viewed_at     TEXT
);
CREATE INDEX idx_secrets_token ON secrets(token);
CREATE INDEX idx_secrets_expires_at ON secrets(expires_at);
CREATE INDEX idx_secrets_user_id ON secrets(user_id);

CREATE TABLE api_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,                   -- human label: "cli-laptop", "ci-runner"
    token_hash    TEXT NOT NULL,                   -- SHA-256 hex of plaintext
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT                             -- non-NULL once revoked
);
CREATE INDEX idx_api_tokens_hash ON api_tokens(token_hash);
CREATE INDEX idx_api_tokens_user_id ON api_tokens(user_id);
CREATE UNIQUE INDEX idx_api_tokens_user_name ON api_tokens(user_id, name);  -- token names unique per-user, not globally

-- Aggregate-only product telemetry. No `user_id` column by design:
-- audit-trail signals belong in app/security_log.py (which DOES carry
-- user_id), not here. Per-event-type payload shape is registered in
-- app/analytics.py::EVENT_REGISTRY. See that module's docstring for the
-- privacy invariant and the test_analytics.py spec for what the table
-- is allowed to carry.
CREATE TABLE analytics_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    occurred_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_analytics_events_type_time ON analytics_events(event_type, occurred_at);

-- Single-row, CHECK-pinned. Stamped by init_db() after every migration
-- finishes. Read on boot so a downgrade onto a DB that a newer release
-- already upgraded fails loudly instead of silently running stale code
-- against new columns.
CREATE TABLE schema_version (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    version  INTEGER NOT NULL
);
```

**`users.totp_secret` is encrypted at rest.** The column holds a
Fernet token prefixed with `v1:`; the KEK is derived via HKDF-SHA256 from
`EPHEMERA_SECRET_KEY` with a stable info string. The model layer encrypts
on `create_user` / `update_user(totp_secret=...)` and decrypts inside
`get_user_by_*`, so auth code still sees the plaintext base32 seed but
backups, raw-SQL queries, and casual DB inspection see only ciphertext.

**Cost to operators:** rotating `EPHEMERA_SECRET_KEY` now bricks every
stored TOTP. The only recovery is for each user to present a recovery
code and run `python -m app.admin rotate-totp`, which writes a fresh
seed encrypted under the new KEK. Plan SECRET_KEY rotations accordingly.

### Migration paths

Two layered mechanisms, in this order, on every `init_db()`:

1. **Legacy add-column-if-missing block** for pre-registry DBs. A small
   ALTER-TABLE pass that backfills:
   - `users.username` (backfilled to `'admin'`), `users.email` (null)
   - `secrets.user_id` (backfilled to 1)
   - `api_tokens.user_id` (backfilled to 1)

   This block exists so a single-user-era DB upgraded into the multi-user
   schema doesn't need a hand-rolled SQL session: the former lone user
   becomes `admin` and every existing row is tagged `user_id=1`. Covered
   by `test_legacy_db_migrates_to_multiuser_schema`. Indices are built
   after this pass so they apply to the new columns.

2. **Versioned migration registry** for everything since. The
   `schema_version` table carries one row stamping the DB's applied
   version. `init_db()`:

   - Reads the stamped version.
   - If the DB version is **higher** than `CURRENT_SCHEMA_VERSION` in the
     code, raises `SchemaVersionError` and refuses to start. The usual
     cause is an operator rolling the app back onto a DB a newer release
     already upgraded; restore a pre-migration backup (or upgrade the
     code) instead of running stale code against the new schema.
   - Otherwise applies every registered migration in ascending order
     from `_MIGRATIONS`, then stamps the DB to `CURRENT_SCHEMA_VERSION`.

   `_MIGRATIONS` is a dict of `{version: callable}` declared in
   `app/models/_core.py`; the callables themselves live one per file
   under `app/models/migrations/v<N>.py`. No Alembic, no autogenerate;
   a dict-of-callables is enough at this scale, and one-file-per-version
   keeps each migration's diff reviewable on its own merits.

The current registry runs through **v6** (`CURRENT_SCHEMA_VERSION = 6`):

| Version | What it does |
|---------|--------------|
| v2      | Add `users.preferred_language` so the locale resolver has a per-user preference column. |
| v3      | Add CHECK constraints on user-controlled TEXT columns (`username`, `passphrase`, `label`) to mirror the Pydantic ceilings as a defense-in-depth boundary. |
| v4      | Create `analytics_events` for product telemetry. |
| v5      | Drop `analytics_events.user_id` (added in v4 then reconsidered: the audit-trail signals belong in `security_log.py`, not in the aggregate table). |
| v6      | Add `users.analytics_opt_in` — per-user consent gate, complementing the operator-level env switch. |

Future schema changes add a new file `v<N+1>.py`, register it in
`_MIGRATIONS`, and bump `CURRENT_SCHEMA_VERSION`.

On reveal:
- If `track = 0`: entire row is deleted.
- If `track = 1`: `ciphertext`, `server_key`, and `passphrase` are set to NULL,
  `status` set to `'viewed'`, `viewed_at` set to current time. Row purged after
  30 days.

## API surface

### Sender (authenticated)

#### `GET /send`
Renders the sender form if a valid session exists, otherwise the login page.
Both are static HTML files.

#### `POST /send/login`
Verifies username + password + TOTP (or recovery code), rotates and sets the
session cookie. Form body: `username`, `password`, `code`. Rate-limited
(10/min per IP); account-locks for one hour after `MAX_FAILURES = 10`
failed attempts on the same user. The failed-attempt counter is monotonic
and persists across attempts (no decay window) — it resets only on a
successful login or after the lockout fires; a slow-burn attacker can't
sit just below the threshold. Returns 401 with an identical body for any
failure reason (wrong username, wrong password, wrong code) except
lockout (423 with the unlock timestamp).

#### `POST /send/logout`
Clears the session cookie. Requires same-origin.

#### `POST /api/secrets`
Creates a new secret. Accepts **either** `Authorization: Bearer <api-token>`
(DB-issued via the admin CLI) **or** a valid session cookie from the web form.

```
Headers: Authorization: Bearer <api-token>       (for programmatic callers)
         -- or session cookie set by POST /send/login (for the web UI)
         Content-Type: application/json          (for text)
                    or multipart/form-data       (for images)

Body (text):
{
  "content": "the secret message",
  "content_type": "text",
  "expires_in": 3600,
  "passphrase": "optional-passphrase",
  "track": false
}

Body (image, multipart):
  file: <image binary>
  expires_in: 3600
  passphrase: (optional)
  track: (optional, default false)

Response 201:
{
  "url": "https://host/s/{token}#{client_half}",
  "id": "<uuid>",
  "expires_at": "2026-04-18T12:00:00Z"
}
```

#### `GET /api/me`
Return a minimal view of the authenticated caller: `{id, username, email,
analytics_opt_in}`. Used by the sender UI to populate the "signed in as …"
header pill and to drive the analytics-toggle's initial state. Authenticates
via the same dependency as the rest of the sender API.

```
Response 200:
{
  "id": <int>,
  "username": "<str>",
  "email": "<str|null>",
  "analytics_opt_in": <bool>
}
Response 401: not authenticated
```

#### `PATCH /api/me/preferences`
Flip user-scoped preferences. Today's only knob is `analytics_opt_in` (the
per-user telemetry-consent gate that complements the operator-level env
switch); the route is shaped as a generic preferences mutation so future
user-scoped settings join without a new endpoint. Returns the same shape
as `GET /api/me`.

A change-detection guard runs the flip in SQL via a conditional `UPDATE`
that returns the new value when it actually fired and `None` on a no-op
(value already matched). The audit log line
(`preferences.analytics_changed`) is emitted only on the firing path, so
flipping back to the current value doesn't generate a phantom audit event.

```
Body: { "analytics_opt_in": true | false | null }   -- null leaves it unchanged
Response 200: <same shape as GET /api/me>
```

#### `PATCH /api/me/language`
Persist the user's preferred UI language as a BCP-47 tag (`'ja'`, `'pt-BR'`,
etc.) on `users.preferred_language`. Passing `null` clears the preference
so locale resolution falls back to the cookie / Accept-Language / project
default chain in `app/i18n.py`.

Authenticated callers only — anonymous callers hit 401 *without* learning
whether the supplied tag is valid (the alternative would leak the
`SUPPORTED` set as a 400-vs-401 oracle to anyone probing).

```
Body: { "language": "ja" }   or { "language": null }
Response 204: persisted
Response 401: not authenticated
Response 400: tag not in the project's SUPPORTED list
              (`{ "code": "unsupported_language", ... }`)
Response 422: malformed request body (FastAPI / Pydantic validation)
```

#### `GET /api/secrets/{id}/status`
Returns status of a tracked secret.

```
Headers: Authorization: Bearer <api-token>  or valid session cookie

Response 200 (tracked):
{ "status": "pending", "created_at": "...", "expires_at": "..." }

Response 200 (viewed):
{ "status": "viewed", "created_at": "...", "viewed_at": "...", "expires_at": "..." }

Response 404: secret not found, not tracked, or purged
```

#### `GET /api/secrets/tracked`
Returns the full list of tracked secrets owned by the authenticated caller.
This is the server-side source of truth for the sender's tracked-list UI
(the localStorage cache used in earlier versions is retired).

```
Headers: Authorization: Bearer <api-token>  or valid session cookie

Response 200:
{
  "items": [
    {
      "id": "<uuid>",
      "content_type": "text" | "image",
      "mime_type": "image/png" | null,
      "label": "API key for Acme" | null,
      "status": "pending" | "viewed" | "burned" | "expired",
      "created_at": "ISO8601",
      "expires_at": "ISO8601",
      "viewed_at": "ISO8601" | null
    },
    ...
  ]
}
```

#### `POST /api/secrets/tracked/clear`
Batch-delete every non-pending tracked row for the caller -- viewed, burned,
canceled, and still-pending-but-past-expiry. Pending live rows are kept.
The UI uses this behind a 2-click-confirm "clear history" action.

```
Headers: Authorization: Bearer <api-token>  or valid session cookie
Response 200: { "cleared": <int> }
Response 401: not authenticated
Response 403: cross-origin (for browser callers)
```

#### `POST /api/secrets/{id}/cancel`
Sender-initiated revocation of a pending secret. The receiver's URL stops
working immediately. Intended for "I sent that link to the wrong person /
changed my mind about sharing".

- Wipes `ciphertext`, `server_key`, and `passphrase` (same as `burn()`).
- If the secret was tracked, the row remains with `status='canceled'`,
  `viewed_at=now`; purged on the normal 30-day retention. This keeps the
  cancellation visible in the tracked list.
- If the secret was not tracked, the row is deleted.
- Returns `404` if the secret doesn't exist, belongs to another user, or
  was already viewed / burned / canceled / expired.

```
Headers: Authorization: Bearer <api-token>  or valid session cookie
Response 204: revoked; the receiver URL now returns 404
Response 401: not authenticated
Response 403: cross-origin (for browser callers)
Response 404: not found or already gone
```

#### `DELETE /api/secrets/{id}`
Removes a secret from the tracked list.

- If the secret is still live (pending), flips `track` to 0 so it stops
  appearing in the list but the receiver URL keeps working.
- If the payload is already gone (viewed / burned / expired), deletes the
  row entirely so the metadata doesn't linger until the 30-day purge.

Idempotent: returns `204` even when the id doesn't exist.

```
Headers: Authorization: Bearer <api-token>  or valid session cookie
Response 204: no body
Response 401: not authenticated
Response 403: cross-origin (for browser callers)
```

### Receiver (unauthenticated)

#### `GET /s/{token}`
Returns the static landing page (always the same HTML). Does not touch the
secret. The page JS calls `/s/{token}/meta` on load and toggles the passphrase
input based on the response.

#### `GET /s/{token}/meta`
Returns whether the secret exists and whether a passphrase is required. Does
not touch the secret. Used only to drive the landing-page UI.

```
Response 200:
{ "passphrase_required": true | false }

Response 404: not found / expired / already viewed / burned
```

#### `POST /s/{token}/reveal`
Reveals and destroys the secret.

```
Body (JSON, sent by JS):
{
  "key": "<client_half from URL fragment>",
  "passphrase": "optional"
}

Response 200 (text):
{ "content_type": "text", "content": "the secret message" }

Response 200 (image):
{ "content_type": "image", "mime_type": "image/png", "content": "<base64>" }

Response 401: wrong passphrase (attempts incremented)
Response 404: not found / expired / already viewed
Response 410: burned (too many passphrase attempts)
Response 429: rate limited
```

The JS on the landing page handles the response: renders text into the page or
creates an `<img>` tag with a `data:` URI for images.

### Operator surface (unauthenticated or auth-gated, not part of the product flow)

#### `GET /healthz`
Liveness + readiness probe. Touches the DB with a no-op query and confirms
`EPHEMERA_SECRET_KEY` is loaded; returns `{"ok": true}` 200 on success and
`{"ok": false, "reason": "<db_unreachable|missing_secret_key>"}` 503 on
failure. Excluded from the OpenAPI schema so unauthenticated probes don't
see it advertised.

The auto-deploy workflow (`.github/workflows/deploy.yml`) polls this after
`systemctl restart` to catch regressions a `/send` smoke test would miss
(broken DB, missing env, WAL permission flip — all surfaces where `/send`
still happily renders its login page).

#### `GET /openapi.json`
The full OpenAPI schema FastAPI would have served by default, gated behind
`Depends(verify_api_token_or_session)` so unauthenticated probes cannot
pull the wire contract (route list, parameter names, schemas). Excluded
from the schema it serves (`include_in_schema=False`).

#### `GET /docs`
Swagger UI rendered against the schema above, gated through the same auth
dependency. Assets under `/static/swagger/` are served locally (pinned
versions; the bundle is generic vendor code) so the page loads under the
strict `script-src 'self'` CSP. Excluded from the OpenAPI schema.

#### `GET /manifest.webmanifest`, `GET /static/manifest.webmanifest`
PWA manifest. Both paths bind to the same handler:
`/manifest.webmanifest` is the canonical URL the layout's
`<link rel="manifest">` points at; `/static/manifest.webmanifest` is a
legacy alias kept so already-installed PWAs whose browsers captured the
old URL keep getting manifest updates instead of silently being told the
manifest disappeared.

The handler is a route rather than a static file because the operator can
flip name + icon variant per environment via `EPHEMERA_DEPLOYMENT_LABEL`;
empty label is the prod posture (`name="ephemera"`, light tile), any
non-empty label produces `ephemera-{label}` with a dark tile so a dev /
staging install on the same phone is at-a-glance distinguishable from
prod.

### `_test/*` (env-gated, e2e-only)

Two POST routes (`/_test/limiter/reset`, `/_test/secret/{token}/expire-now`)
are registered only when `EPHEMERA_E2E_TEST_HOOKS=1` is set in the
process environment (or `.env`). They give the Playwright suite hooks
to reset the in-memory limiter and force-expire a secret without sleeping
through real-time. Production deploys never set the flag, so the
`/_test/*` routes are not registered and don't exist on the wire. Both
routes carry `verify_same_origin` + `read_rate_limit` and live in
`app/_test_hooks.py`; the registration site in `app/__init__.py` is the
gate.

## Project structure

Server-side layout (the browser files under `app/static/` and the
`app/templates/` Jinja sources are covered in [`frontend.md`](frontend.md)):

```
ephemera/
  app/
    __init__.py             # FastAPI app factory, lifespan, route mounts, /healthz, /docs, manifest, test-hook gate
    config.py               # pydantic-settings; env vars + .env, with `e2e_test_hooks` and friends
    crypto.py               # Fernet key gen, key-half split, encrypt / decrypt; HKDF-derived KEK for at-rest TOTP
    validation.py           # MIME / file-magic / size validation
    cleanup.py              # Async background task: expired purge + 30-day tracked purge
    dependencies.py         # FastAPI deps: session cookie, api-token-or-session, verify_same_origin
    limiter.py              # In-memory sliding-window limiters: reveal, login, create, read
    errors.py               # http_error() + canonical { code, message } error response shape
    schemas.py              # Pydantic request / response models with documented length ceilings
    i18n.py                 # Locale resolution, gettext loading, template_context() injector
    analytics.py            # Aggregate-only event registry + emit() (privacy invariants in module docstring)
    security_headers.py     # The CSP / HSTS / Permissions-Policy block applied as middleware
    security_log.py         # Structured audit log: emit() + the security_log handler shape
    _test_hooks.py          # /_test/* routes; only registered when EPHEMERA_E2E_TEST_HOOKS=1
    auth/                   # Auth package (split by concern)
      _core.py              # Password hash + verify, BCRYPT_ROUNDS pin, AuthError canonical exception
      lockout.py            # Per-user failure tracking + lockout window
      login.py              # The full authenticate() pipeline (password + TOTP, recovery codes, generic-creds error surface)
      totp.py               # pyotp with ±1 step + anti-replay via totp_last_step
      recovery_codes.py     # Generation, hashing, single-use marking
      tokens.py             # API-token mint + lookup (SHA-256 digest, indexed)
      hibp.py               # SHA-1 k-anonymity range API client (offline -> fail-open)
    admin/                  # CLI package; same split-by-concern shape
      cli.py                # Argparse + COMMANDS dispatch table + main()
      _core.py              # Shared prompts, _provision_user, _reauth, audit() re-export
      users.py              # init, add-user, list-users, remove-user, reset-password
      rotation.py           # rotate-totp, regen-recovery-codes
      tokens.py             # create-token, list-tokens, revoke-token
      diagnostics.py        # diagnose, verify, analytics-summary
    models/                 # DB layer (sqlite3, no ORM)
      _core.py              # Schema, init_db, _connect, _utcnow, _iso, schema_version
      secrets.py            # CRUD + lifecycle (cancel, untrack, list_tracked_secrets, etc.)
      users.py              # CRUD + at-rest TOTP encryption + analytics_opt_in atomic flip
      api_tokens.py         # Create / lookup / revoke / touch_last_used
      migrations/
        v2.py               # Add users.preferred_language
        v3.py               # CHECK constraints on user-controlled TEXT columns
        v4.py               # Create analytics_events
        v5.py               # Drop analytics_events.user_id
        v6.py               # Add users.analytics_opt_in
    routes/
      sender.py             # /send + /send/login + /send/logout + /api/secrets family
      receiver.py           # GET /s/{token} + GET /s/{token}/meta + POST /s/{token}/reveal
      prefs.py              # GET /api/me + PATCH /api/me/preferences + PATCH /api/me/language
    templates/              # Jinja shells: _layout.html, _docs.html, login.html, sender.html, landing.html
    static/                 # JS + CSS + i18n catalogues + Swagger vendor bundle (see frontend.md)
    translations/           # gettext .po / .mo per supported locale
  tests/                    # pytest suite (see AGENTS.md §3 for the spec-vs-implementation distinction)
    conftest.py             # Session bcrypt-cost-4 safety net + every product fixture
    test_admin.py           # Admin CLI behaviours
    test_analytics.py       # Aggregate-only telemetry invariants
    test_auth.py            # Password / TOTP / recovery codes / lockout / API tokens / HIBP
    test_chrome_variant.py  # Sender vs receiver chrome divergences
    test_cleanup.py         # Expired purge + tracked-metadata purge
    test_config.py          # pydantic-settings parsing, env-var routing
    test_crypto.py          # Fernet key gen, split, encrypt/decrypt round-trips
    test_fitness_functions.py  # AST-level architecture invariants (rate-limit on every state-mutating route, etc.)
    test_i18n.py            # Locale resolution, catalog presence, RTL handling
    test_models.py          # CRUD + expiry + tracking + multi-user scoping
    test_pwa.py             # Manifest shape per EPHEMERA_DEPLOYMENT_LABEL
    test_receiver.py        # Landing + reveal + passphrase + burn-on-fail
    test_security.py        # Security headers, CSP, origin gate, generic-creds invariant, /healthz
    test_security_log.py    # Audit log emission + field schemas
    test_sender.py          # Login, logout, create, status, tracked list, prefs, labels
    test_test_hooks.py      # /_test/* router (env-gated; tests run with the gate on)
    test_validation.py      # MIME / magic / size / SVG-rejection
  tests-e2e/                # Playwright acceptance suite — the system's spec layer (AGENTS.md §3)
  tests-js/                 # Vitest front-end unit tests
  scripts/                  # release.sh, deploy/, i18n.sh, generate-pwa-icons.py, crap_report.py
  requirements.txt          # runtime deps; pip-compile --generate-hashes pinned
  requirements-dev.txt      # runtime + pytest + pytest-cov + httpx + hypothesis (lockfile, hashed)
  run.py                    # Dev entrypoint: uvicorn app:create_app --reload --factory
  .env.example              # Template for the env-var surface; commented per-knob
```

**Note on the templates layer.** The `app/templates/` directory carries the
chrome shells (`_layout.html`, `_docs.html`) and the page-level templates
(`login.html`, `sender.html`, `landing.html`). The revealed content of a
secret is *not* rendered into any of them: `reveal.js` paints the JSON
response from `POST /s/{token}/reveal` directly into the DOM, so the
plaintext never lives as a server-rendered HTML attribute, never appears
in any rendered-page log line, and isn't cached by any intermediary that
trusts HTML responses. Templates render only what the server already owns
(chrome, locale-resolved labels, the static landing-page shell); the
zero-knowledge property is a function of the chrome / payload split, not
of templating-versus-no-templating.
