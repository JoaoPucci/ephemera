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
| Templating       | None (static HTML)          | Pages are either static or render client-owned data; see note below |
| Process mgmt     | systemd                     | Learning goal, Docker migration later           |
| Testing          | pytest + httpx              | httpx for FastAPI TestClient                    |

**Note on no server-side templating**: Server-side rendering is useful when the
server has dynamic data to bake into HTML. Our pages are either fully static
(login, sender, gone) or display data the server must not see in HTML (the
revealed secret is returned as JSON from the reveal POST and rendered by JS).
The one piece of server-owned per-page data -- whether a secret requires a
passphrase -- is delivered via a small `GET /s/{token}/meta` JSON endpoint that
the landing-page JS calls on load. Less code, smaller attack surface, no
dependency on Jinja2.

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

- Rate limiting: 10 requests/minute per IP on reveal endpoint (via `slowapi`,
  a Starlette/FastAPI rate-limiting library built on `limits`).
- CSRF: The reveal action is a JSON POST initiated by JS (not a form submit).
  The `Origin` header is validated server-side against the configured host. Since
  the client half of the key is required in the body (only available to JS running
  on the page), this acts as a natural CSRF barrier.
- Security headers on all responses (via FastAPI middleware):
  - `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:`
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: no-referrer`
  - `Strict-Transport-Security: max-age=86400` (conservative first-rollout value;
    HSTS is sticky, so start small, then bump to `max-age=31536000; includeSubDomains; preload`
    once the cert has survived at least one Let's Encrypt renewal. Caddy does *not* add
    HSTS automatically -- only the app middleware sets it.)
- No secret content in logs: Uvicorn access log format configured to exclude
  request bodies. FastAPI exception handlers scrub sensitive data.
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

Per-session rate limit (applies to authenticated callers):
- `POST /api/secrets`: 60 / hour per session (contains blast radius of a
  hijacked session).

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
CSRF gap closed by F-03. `SameSite=Strict` remains the primary defense;
the Origin check is a second layer.

## Database schema

Single table, kept minimal:

```sql
CREATE TABLE secrets (
    id            TEXT PRIMARY KEY,   -- UUID4 (for sender status lookups)
    token         TEXT UNIQUE NOT NULL,-- URL-safe random token (for receiver URLs)
    server_key    BLOB,                -- server half of the Fernet key (16 bytes, NULL after reveal if tracked)
    ciphertext    BLOB,                -- encrypted payload (NULL after reveal if tracked)
    content_type  TEXT NOT NULL,       -- 'text' or 'image'
    mime_type     TEXT,                -- 'image/png', etc. NULL for text
    passphrase    TEXT,                -- bcrypt hash, NULL if no passphrase
    track         INTEGER DEFAULT 0,  -- whether to keep metadata after reveal
    status        TEXT DEFAULT 'pending', -- 'pending', 'viewed', 'burned', 'canceled', 'expired'
    attempts      INTEGER DEFAULT 0,  -- failed passphrase attempts
    label         TEXT,                -- sender-supplied nickname for the tracked list (NULL if untracked or unset)
    created_at    TEXT NOT NULL,       -- ISO8601 UTC
    expires_at    TEXT NOT NULL,       -- ISO8601 UTC
    viewed_at     TEXT                 -- ISO8601 UTC, set on reveal
);

CREATE INDEX idx_secrets_token ON secrets(token);
CREATE INDEX idx_secrets_expires_at ON secrets(expires_at);

CREATE TABLE users (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    username              TEXT NOT NULL,           -- unique via idx_users_username
    email                 TEXT,                    -- unique-if-set; nullable until email flows land
    password_hash         TEXT NOT NULL,           -- bcrypt, cost 12
    totp_secret           TEXT NOT NULL,           -- base32, 32 chars
    totp_last_step        INTEGER DEFAULT 0,       -- anti-replay: reject step <= this
    recovery_code_hashes  TEXT DEFAULT '[]',       -- JSON: [{"hash": bcrypt, "used_at": ISO8601|null}]
    failed_attempts       INTEGER DEFAULT 0,
    lockout_until         TEXT,                    -- ISO8601 or NULL
    created_at, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_users_username ON users(username);
CREATE UNIQUE INDEX idx_users_email    ON users(email) WHERE email IS NOT NULL;

-- secrets.user_id ties every secret to its creator. ON DELETE CASCADE drops
-- a user's secrets when the user is removed.
ALTER TABLE secrets ADD COLUMN user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE;
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
```

### Migration from the single-user era

`init_db()` runs a small ALTER-TABLE-ADD-COLUMN migration on existing DBs:
- `users.username` (backfilled to `'admin'`), `users.email` (null)
- `secrets.user_id` (backfilled to 1)
- `api_tokens.user_id` (backfilled to 1)

Indices are created after migration so they apply to the new columns. The
legacy single-user DB continues to work, with the former lone user renamed
`admin` and all their data tagged `user_id=1`. Covered by
`test_legacy_db_migrates_to_multiuser_schema`.

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
(10/min per IP) with account lockout after 10 failures in 15 minutes (per
user). Returns 401 with an identical body for any failure reason (wrong
username, wrong password, wrong code) except lockout (423 with the unlock
timestamp).

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
Return a minimal view of the authenticated caller: `{id, username, email}`.
Used by the sender UI to populate the "signed in as …" header pill without
needing server-side templating. Authenticates via the same dependency as the
rest of the sender API.

```
Response 200:
{ "id": <int>, "username": "<str>", "email": "<str|null>" }
Response 401: not authenticated
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

## Project structure

Server-side layout (the browser files under `app/static/` are covered in
[`frontend.md`](frontend.md)):

```
ephemera/
  app/
    __init__.py            # FastAPI app factory, lifespan, security headers middleware
    config.py              # Configuration from env vars with defaults (pydantic-settings)
    models.py              # DB init + CRUD for secrets, users, api_tokens; lightweight migrations
    crypto.py              # Key generation, splitting, encrypt/decrypt
    validation.py          # MIME checking, file magic validation, size limits
    auth.py                # Password, TOTP (pyotp) with +/-1 step + anti-replay, recovery codes, lockout, API-token mint/lookup
    admin.py               # CLI: init, reset-password, rotate-totp, regen-recovery-codes, create/list/revoke tokens, diagnose, verify
    cleanup.py             # Async background task for expired + 30-day tracked purge
    dependencies.py        # FastAPI dependencies: session cookie, api-token-or-session, origin check
    limiter.py             # In-memory sliding-window rate limiters (login, reveal, create)
    routes/
      __init__.py
      sender.py            # /send family + /api/secrets (create, status, list tracked, delete)
      receiver.py          # GET /s/{token}, GET /s/{token}/meta, POST /s/{token}/reveal
    static/                # (see frontend.md)
  tests/
    conftest.py            # Fixtures: sync TestClient, isolated DB, provisioned user, API token
    test_crypto.py         # Key gen, split, encrypt, decrypt, round-trip
    test_validation.py     # MIME check, magic bytes, size limits, SVG rejection
    test_models.py         # CRUD, expiry, tracking, deletion behavior
    test_auth.py           # Password verify, TOTP skew+replay, recovery codes, lockout, tokens
    test_sender.py         # Login, logout, create, status, tracked list, delete, labels
    test_receiver.py       # Landing page, reveal flow, passphrase flow, burn-on-fail
    test_cleanup.py        # Expired purge, tracked metadata purge
    test_security.py       # Headers, rate limiting, origin validation
  requirements.txt         # runtime deps only (installed on the server)
  requirements-dev.txt     # runtime + pytest / pytest-cov / httpx (local)
  run.py                   # Dev entrypoint: uvicorn app:create_app --reload
  .env.example             # Template for required env vars
```

**Template simplification**: The revealed content (text and image) is rendered
client-side by `reveal.js` after the JSON response from the reveal endpoint.
No separate `revealed_text.html` / `revealed_image.html` templates needed --
the landing page transforms in place. Similarly, `sender.js` handles the
success state inline, eliminating `sender_result.html`.
