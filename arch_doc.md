# Ephemera - One-Time Secret System

## Overview

A self-hosted one-time secret (OTS) sharing system. Secrets (text or images) are
encrypted at rest, viewable exactly once, and destroyed after viewing or expiry.

---

## Decisions Log

| # | Question                  | Decision                                                    |
|---|---------------------------|-------------------------------------------------------------|
| 1 | Sender interface          | Web form at `/send`, password + TOTP login, signed session cookie |
| 2 | Encryption model          | Key splitting -- half in DB, half in URL fragment            |
| 3 | Receiver passphrase       | Optional, set by sender at creation time                    |
| 4 | Image size limit          | 10 MB                                                       |
| 5 | Burn confirmation         | Optional status endpoint, opt-in at creation time           |
| 6 | Database                  | SQLite                                                      |
| 7 | Image formats             | PNG, JPEG, GIF, WebP only. SVG rejected.                    |
| 8 | Deployment                | Uvicorn + Caddy + systemd (Docker migration later)          |
| 9 | Sender authentication     | bcrypt password + TOTP with ±1 step + backup codes; lockout after 10 fails in 15 min |
| 10| External API auth         | DB-issued named tokens (SHA-256 hash stored), revocable, replace the old static `EPHEMERA_API_KEY` |
| 11| Provisioning              | CLI tool (`python -m app.admin init`); no web setup wizard   |

---

## Roles

- **Sender**: Single user. Creates secrets via web form at `/send`.
- **Receiver**: Anyone with the link. Sees an explanation page, clicks to reveal,
  secret is destroyed immediately after.

---

## Core Flow

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

---

## Expiry Presets

| Label       | Duration |
|-------------|----------|
| 5 minutes   | 300s     |
| 30 minutes  | 1800s    |
| 1 hour      | 3600s    |
| 4 hours     | 14400s   |
| 12 hours    | 43200s   |
| 24 hours    | 86400s   |
| 3 days      | 259200s  |
| 7 days      | 604800s  |

Default: 24 hours.

---

## Tech Stack

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
| Frontend         | Plain HTML/CSS/JS           | No build step, fast for receiver                |

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

---

## Security Design

### Key Splitting (zero-knowledge encryption)

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
  - `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'`
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: no-referrer`
  - `Strict-Transport-Security: max-age=31536000` (Caddy handles this automatically)
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
`POST /send/logout`, `POST /api/secrets`, `POST /s/{token}/reveal`). Requests
from a foreign Origin get 403. Missing Origin (e.g., curl, CLI) is allowed;
the cookie's `SameSite=Strict` plus the bearer-token model handle CSRF for
browsers.

---

## Database Schema

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
    status        TEXT DEFAULT 'pending', -- 'pending', 'viewed', 'burned', 'expired'
    attempts      INTEGER DEFAULT 0,  -- failed passphrase attempts
    label         TEXT,                -- sender-supplied nickname for the tracked list (NULL if untracked or unset)
    created_at    TEXT NOT NULL,       -- ISO8601 UTC
    expires_at    TEXT NOT NULL,       -- ISO8601 UTC
    viewed_at     TEXT                 -- ISO8601 UTC, set on reveal
);

CREATE INDEX idx_secrets_token ON secrets(token);
CREATE INDEX idx_secrets_expires_at ON secrets(expires_at);

CREATE TABLE users (
    id                    INTEGER PRIMARY KEY,   -- always 1 (single-user)
    password_hash         TEXT NOT NULL,          -- bcrypt, cost 12
    totp_secret           TEXT NOT NULL,          -- base32, 32 chars
    totp_last_step        INTEGER DEFAULT 0,      -- anti-replay: reject step <= this
    recovery_code_hashes  TEXT DEFAULT '[]',       -- JSON: [{"hash": bcrypt, "used_at": ISO8601|null}]
    failed_attempts       INTEGER DEFAULT 0,
    lockout_until         TEXT,                    -- ISO8601 or NULL
    created_at, updated_at TEXT NOT NULL
);

CREATE TABLE api_tokens (
    id            INTEGER PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,            -- human label: "cli-laptop", "ci-runner"
    token_hash    TEXT NOT NULL,                   -- SHA-256 hex of plaintext
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT                             -- non-NULL once revoked
);
CREATE INDEX idx_api_tokens_hash ON api_tokens(token_hash);
```

On reveal:
- If `track = 0`: entire row is deleted.
- If `track = 1`: `ciphertext`, `server_key`, and `passphrase` are set to NULL,
  `status` set to `'viewed'`, `viewed_at` set to current time. Row purged after
  30 days.

---

## Project Structure

```
ephemera/
  app/
    __init__.py            # FastAPI app factory, lifespan, security headers middleware
    config.py              # Configuration from env vars with defaults (pydantic-settings)
    models.py              # DB init, secret CRUD operations
    crypto.py              # Key generation, splitting, encrypt/decrypt
    validation.py          # MIME checking, file magic validation, size limits
    cleanup.py             # Async background task for expired secret purge
    dependencies.py        # FastAPI dependencies: auth, rate limiting, session
    routes/
      __init__.py
      sender.py            # /send family + /api/secrets (create, status, list tracked, delete)
      receiver.py          # GET /s/{token}, GET /s/{token}/meta, POST /s/{token}/reveal
    static/
      login.html           # API key entry for /send (served via FileResponse)
      sender.html          # Secret creation form (text/image, expiry, passphrase, track)
      landing.html         # Receiver: explanation + reveal button; JS toggles passphrase UI
      gone.html            # Secret expired, already viewed, or burned (fallback)
      style.css            # Clean, minimal styling
      reveal.js            # Calls /meta, reads URL fragment, sends reveal POST, renders result
      sender.js            # Tab toggle, drag-drop, form submission, result display
  tests/
    conftest.py            # Fixtures: httpx AsyncClient, test DB, sample secrets
    test_crypto.py         # Key gen, split, encrypt, decrypt, round-trip
    test_validation.py     # MIME check, magic bytes, size limits, SVG rejection
    test_models.py         # CRUD, expiry, tracking, deletion behavior
    test_sender.py         # Auth, form rendering, secret creation, status endpoint
    test_receiver.py       # Landing page, reveal flow, passphrase flow, burn-on-fail
    test_cleanup.py        # Expired purge, tracked metadata purge
    test_security.py       # Headers, rate limiting, origin validation
  requirements.txt
  run.py                   # Dev entrypoint: uvicorn app:create_app --reload
  .env.example             # Template for required env vars
  Caddyfile                # Production Caddy config (reverse proxy + auto-TLS)
  ephemera.service         # systemd unit file
```

**Template simplification**: The revealed content (text and image) is rendered
client-side by `reveal.js` after the JSON response from the reveal endpoint.
No separate `revealed_text.html` / `revealed_image.html` templates needed --
the landing page transforms in place. Similarly, `sender.js` handles the
success state inline, eliminating `sender_result.html`.

---

## API Surface

### Sender (authenticated)

#### `GET /send`
Renders the sender form if a valid session exists, otherwise the login page.
Both are static HTML files.

#### `POST /send/login`
Verifies password + TOTP (or recovery code), rotates and sets the session cookie.
Form body: `password`, `code`. Rate-limited (10/min per IP) with account
lockout after 10 failures in 15 minutes. Returns 401 with an identical body for
any failure reason except lockout (423 with the unlock timestamp).

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

---

## UI Design

### Design Philosophy

The name "ephemera" refers to things that are transient and fleeting -- old
tickets, handwritten notes, letters meant to be read once. The UI leans into
this: quiet, paper-like, unhurried. No flashy gradients or corporate SaaS
energy. It should feel like unsealing an envelope, not logging into a dashboard.

### Visual Language

**Palette**

Two themes share the same semantic tokens; only the concrete values differ.
Defined as CSS custom properties on `:root` (light) and `[data-theme="dark"]`.

_Light theme_

| Role        | Value     | Usage                                          |
|-------------|-----------|-------------------------------------------------|
| Background  | `#fafafa` | Page background -- neutral off-white           |
| Surface     | `#ffffff` | Card/container background                      |
| Text        | `#09090b` | Body text -- near-black                        |
| Text muted  | `#71717a` | Secondary text, captions, hints                |
| Accent      | `#4f46e5` | Buttons, links, active states -- indigo, like ink |
| Accent hover| `#4338ca` | Button hover, slightly deeper                  |
| Border      | `#e4e4e7` | Card borders, dividers                         |
| Danger      | `#dc2626` | Error states, burn warnings                    |
| Success     | `#16a34a` | Confirmation, "secret created" feedback        |

_Dark theme_

| Role        | Value     | Usage                                          |
|-------------|-----------|-------------------------------------------------|
| Background  | `#09090b` | Page background -- near-black                  |
| Surface     | `#18181b` | Card/container background                      |
| Text        | `#fafafa` | Body text -- soft off-white                    |
| Text muted  | `#a1a1aa` | Secondary text, captions, hints                |
| Accent      | `#818cf8` | Brighter indigo for contrast on dark bg        |
| Accent hover| `#a5b4fc` | Button hover                                    |
| Border      | `#27272a` | Card borders, dividers                         |
| Danger      | `#f87171` | Error states                                    |
| Success     | `#4ade80` | Confirmation                                    |

Pill backgrounds are derived from the accent/success/danger tokens via
`color-mix()`, so both themes stay consistent without duplicated values.

### Theme switching

On first visit the theme is chosen from `prefers-color-scheme`. The user can
toggle between light and dark via a small fixed button in the top-right
corner; the choice is persisted in `localStorage` under `ephemera_theme_v1`.
The theme script is loaded in `<head>` and sets `data-theme` on the root
element before the body renders, so there is no flash of wrong theme.

**Typography**

System font stack only -- no external font loads (faster, no third-party
requests, better privacy):

```css
font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
             "Helvetica Neue", Arial, sans-serif;
```

- Base size: `17px` (slightly larger than default for readability)
- Headings: `600` weight, same stack
- Secret text display: monospace stack for text secrets (signals "this is the
  verbatim content")
- Line height: `1.6` for body, `1.3` for headings

**Spacing and Layout**

- Single centered column, `max-width: 520px`, generous horizontal padding
- Card container: white background, `1px` border in `#e8e4dd`, `border-radius: 8px`,
  subtle shadow (`0 1px 3px rgba(0,0,0,0.04)`)
- Vertical rhythm: `1.5rem` between sections, `2.5rem` above/below the card
- The app name "ephemera" appears as a small, muted wordmark centered above the
  card -- not a loud logo, just a quiet identifier

**Interactions**

- Buttons: solid accent background, white text, `border-radius: 6px`, `padding: 12px 32px`.
  On hover: slightly darker accent + subtle lift (`translateY(-1px)` + shadow).
  Transition: `150ms ease`.
- Inputs: clean bordered fields, same radius as buttons, no inner shadow. Focus
  state: accent-colored border, no outline glow.
- The reveal button is the only loud element on the receiver page. Everything
  else is deliberately quiet so attention goes there.
- After reveal, the content fades in (`opacity 0 -> 1`, `300ms`). Subtle, not
  theatrical.

### Page-by-Page Layout

#### Receiver: Landing (`/s/{token}`)

The most important page. First thing the receiver sees.

```
+-------------------------------------------+
|             ephemera                       |  <- muted wordmark, centered
|                                           |
|  +-------------------------------------+  |
|  |                                     |  |
|  |  Someone shared a secret with you.  |  |  <- heading, centered
|  |                                     |  |
|  |  This message can only be viewed    |  |  <- body text, centered
|  |  once. After you reveal it, it      |  |
|  |  will be permanently destroyed.     |  |
|  |                                     |  |
|  |  +-------------------------------+  |  |  <- only if passphrase-protected
|  |  |  Enter passphrase             |  |  |
|  |  +-------------------------------+  |  |
|  |                                     |  |
|  |       [ Reveal Secret ]             |  |  <- prominent accent button
|  |                                     |  |
|  +-------------------------------------+  |
|                                           |
+-------------------------------------------+
```

- If the secret is not passphrase-protected, the passphrase field is absent
  (not hidden, not disabled -- absent from the DOM).
- No mention of technical details (encryption, key splitting). The receiver
  doesn't need to know or care.

#### Receiver: Revealed Text

```
+-------------------------------------------+
|             ephemera                       |
|                                           |
|  +-------------------------------------+  |
|  |                                     |  |
|  |  +-------------------------------+  |  |
|  |  | The secret message content    |  |  |  <- monospace, light bg (#f8f6f1),
|  |  | displayed here, preserving    |  |  |     padding, left-aligned,
|  |  | whitespace and line breaks.   |  |  |     word-wrap
|  |  +-------------------------------+  |  |
|  |                                     |  |
|  |          [ Copy to clipboard ]      |  |  <- secondary style button, optional
|  |                                     |  |
|  |  This secret has been destroyed.    |  |  <- muted text, centered
|  |                                     |  |
|  +-------------------------------------+  |
|                                           |
+-------------------------------------------+
```

- Copy button uses the Clipboard API. If unsupported, the button is absent.
- The destruction notice is calm, factual, not dramatic.

#### Receiver: Revealed Image

```
+-------------------------------------------+
|             ephemera                       |
|                                           |
|  +-------------------------------------+  |
|  |                                     |  |
|  |  +-------------------------------+  |  |
|  |  |                               |  |  |
|  |  |        [ image ]              |  |  |  <- max-width: 100%, auto height,
|  |  |                               |  |  |     border-radius: 4px
|  |  +-------------------------------+  |  |
|  |                                     |  |
|  |  This secret has been destroyed.    |  |
|  |                                     |  |
|  +-------------------------------------+  |
|                                           |
+-------------------------------------------+
```

- Image is rendered inline as `<img src="data:{mime};base64,...">`.
- Card max-width expands to `680px` for images to give them room.
- Very tall images are capped with `max-height: 80vh` and `object-fit: contain`.

#### Receiver: Gone / Expired

```
+-------------------------------------------+
|             ephemera                       |
|                                           |
|  +-------------------------------------+  |
|  |                                     |  |
|  |  This secret is no longer           |  |  <- heading
|  |  available.                         |  |
|  |                                     |  |
|  |  It may have already been viewed    |  |  <- body, muted
|  |  or has expired.                    |  |
|  |                                     |  |
|  +-------------------------------------+  |
|                                           |
+-------------------------------------------+
```

- No error codes, no technical jargon. Just a clear, calm message.
- Same visual treatment as the other pages -- consistent card, same spacing.

#### Sender: Login (`/send` when unauthenticated)

```
+-------------------------------------------+
|             ephemera                       |
|                                           |
|  +-------------------------------------+  |
|  |                                     |  |
|  |  +-------------------------------+  |  |
|  |  |  API key                      |  |  |
|  |  +-------------------------------+  |  |
|  |                                     |  |
|  |           [ Sign in ]               |  |
|  |                                     |  |
|  +-------------------------------------+  |
|                                           |
+-------------------------------------------+
```

- Password-type input (masked).
- No "forgot password" or signup -- single user, if you don't know the key
  there's nothing here for you.

#### Sender: Create Secret (`/send` when authenticated)

```
+-------------------------------------------+
|             ephemera                       |
|                                           |
|  +-------------------------------------+  |
|  |                                     |  |
|  |  [ Text ]  [ Image ]               |  |  <- tab toggle (not <a>, just
|  |                                     |  |     styled buttons swapping panels)
|  |  +-------------------------------+  |  |
|  |  | Enter your secret...          |  |  |  <- textarea (text tab)
|  |  |                               |  |  |
|  |  |                               |  |  |
|  |  +-------------------------------+  |  |
|  |                                     |  |
|  |  -- or when Image tab active: --    |  |
|  |  +-------------------------------+  |  |
|  |  |                               |  |  |  <- drop zone with dashed border,
|  |  |   Drop image here or click    |  |  |     accepts click for file dialog
|  |  |   to browse                   |  |  |
|  |  |                               |  |  |
|  |  +-------------------------------+  |  |
|  |                                     |  |
|  |  Expires in: [ 24 hours      v ]   |  |  <- dropdown with presets
|  |                                     |  |
|  |  Passphrase (optional):             |  |
|  |  +-------------------------------+  |  |
|  |  |                               |  |  |
|  |  +-------------------------------+  |  |
|  |                                     |  |
|  |  [ ] Track viewing status           |  |  <- checkbox, unchecked by default
|  |                                     |  |
|  |       [ Create Secret ]             |  |
|  |                                     |  |
|  +-------------------------------------+  |
|                                           |
+-------------------------------------------+
```

- Tab toggle between text and image is pure JS (swap `display:none`), no page
  reload.
- Image drop zone shows a thumbnail preview after selection, with file name and
  size. A small "x" clears the selection.
- Passphrase field is a regular text input (not masked -- the sender should see
  what they're typing since they need to communicate it to the receiver).
- The form submits via JS (`fetch`) so the page doesn't reload.

#### Sender: Secret Created (success state)

Replaces the form card content after creation (no page navigation):

```
+-------------------------------------------+
|             ephemera                       |
|                                           |
|  +-------------------------------------+  |
|  |                                     |  |
|  |  Secret created.                    |  |  <- success color heading
|  |                                     |  |
|  |  +-------------------------------+  |  |
|  |  | https://host/s/abc...#key123  |  |  |  <- selectable, monospace,
|  |  +-------------------------------+  |  |     light bg
|  |            [ Copy URL ]             |  |
|  |                                     |  |
|  |  Expires: April 18, 2026 at 14:00  |  |  <- human-readable
|  |  Tracking: enabled                  |  |  <- only if track was checked
|  |                                     |  |
|  |       [ Create Another ]            |  |  <- resets the form
|  |                                     |  |
|  +-------------------------------------+  |
|                                           |
+-------------------------------------------+
```

### Responsive Behavior

- The 520px card + padding works on any screen >= 360px wide.
- On narrow screens (<480px): card loses horizontal margin and side border-radius,
  becomes full-width with top/bottom margin only.
- Touch targets: all buttons and inputs are at least 44px tall (iOS/Android
  accessibility minimum).
- No horizontal scroll at any viewport size.

### What the UI Does NOT Have

- No animations beyond the reveal fade-in, status pulse, and button hover transitions.
- No JavaScript frameworks. Vanilla JS only, under 100 lines total.
- No external resources (fonts, CDNs, analytics, icons). Fully self-contained.
- No footer, no "powered by", no version number. The page is just the card.

---

## Implementation Order

Each step includes its corresponding tests. Tests are written alongside the
implementation, not after.

### Phase 1: Foundation
1. **Project setup**: `requirements.txt`, `run.py`, `.env.example`, app factory
   with lifespan, security headers middleware
2. **`config.py`**: Settings class using pydantic-settings, loaded from env vars
3. **`crypto.py` + `test_crypto.py`**: Key generation, splitting, reconstruction,
   Fernet encrypt/decrypt, round-trip tests, edge cases (wrong key, corrupted
   ciphertext)
4. **`validation.py` + `test_validation.py`**: MIME whitelist, magic byte
   detection, size limit enforcement, SVG rejection

### Phase 2: Data Layer
5. **`models.py` + `test_models.py`**: DB init, create/read/delete secret,
   tracking behavior, expiry queries

### Phase 3: Routes + Auth
6. **`dependencies.py` + sender routes + `test_sender.py`**: API key dependency,
   session cookie dependency, login, `POST /api/secrets` for text and image,
   status endpoint
7. **`receiver.py` + `test_receiver.py`**: Landing page, reveal flow, passphrase
   verification, burn-after-failed-attempts, error states
8. **`test_security.py`**: Security headers, rate limiting, origin validation

### Phase 4: Frontend
9. **Templates + `reveal.js` + `sender.js` + `style.css`**: All HTML templates,
   JS for fragment reading, reveal POST, sender form handling, clean minimal CSS

### Phase 5: Ops
10. **`cleanup.py` + `test_cleanup.py`**: Async background task via lifespan,
    expired secret purge, tracked metadata cleanup
11. **`Caddyfile`**: Reverse proxy config with automatic TLS
12. **`ephemera.service`**: systemd unit file for Uvicorn

---

## Deployment Architecture (systemd)

```
                    Internet
                       |
                       v
                 +-----+------+
                 |   Caddy     |  Automatic TLS (Let's Encrypt),
                 |  (reverse   |  static file serving,
                 |   proxy)    |  request size limit (10MB)
                 +-----+------+
                       |
                  localhost:8000
                       |
                       v
                 +-----+------+
                 |  Uvicorn    |  ASGI server, managed by systemd
                 |             |  single worker (see note below)
                 +-----+------+
                       |
                       v
                 +-----+------+
                 |  FastAPI    |  Ephemera app
                 |  + SQLite   |  DB file in /var/lib/ephemera/
                 +-------------+
```

**Caddyfile** (included in repo):

```
your-domain.com {
    reverse_proxy localhost:8000
    request_body {
        max_size 10MB
    }
}
```

That's it. Caddy handles TLS certificate provisioning, renewal, HTTPS
redirects, and HSTS headers automatically. No certbot, no cron, no manual
cert paths.

**Why one Uvicorn worker**: The "2 * CPU + 1" formula is a Gunicorn heuristic
for CPU-bound synchronous WSGI apps -- it doesn't apply here. Uvicorn is async:
a single worker handles I/O concurrency via the event loop, so it can serve
many concurrent requests without spawning extra processes. This app is I/O-bound
(SQLite reads, network), not CPU-bound. Additionally, multiple workers means
multiple OS processes, which means contention on SQLite's process-level write
lock. One worker avoids that entirely. On a 1 vCPU droplet with low-volume
personal use, one worker is the correct choice.

**systemd unit** (`ephemera.service`, included in repo):

```ini
[Unit]
Description=Ephemera OTS
After=network.target

[Service]
Type=exec
User=ephemera
Group=ephemera
WorkingDirectory=/opt/ephemera
EnvironmentFile=/etc/ephemera/env
ExecStart=/opt/ephemera/venv/bin/uvicorn app:create_app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**File locations**:
- App code: `/opt/ephemera/`
- Virtual env: `/opt/ephemera/venv/`
- Database: `/var/lib/ephemera/ephemera.db`
- Env file: `/etc/ephemera/env`
- systemd unit: `/etc/systemd/system/ephemera.service`
- Caddyfile: `/etc/caddy/Caddyfile`

Claude sessions used:
- claude --resume ec39eb3e-606b-4091-bdd6-74ef8b74c3bd
