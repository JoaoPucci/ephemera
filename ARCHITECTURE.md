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
| 12| Tracked-secrets storage   | Server-authoritative list via `/api/secrets/tracked`; localStorage only caches `{id: url}` because the URL fragment never leaves the creating browser |
| 13| Tracked-list refresh      | Client polls `/api/secrets/tracked` every 5 s while any item is pending; diff-based re-render skips DOM churn; polling stops when nothing is pending |
| 14| Theme                     | Light (default) + dark via CSS custom properties on `[data-theme]`; user choice persisted in localStorage; `prefers-color-scheme` on first visit |
| 15| Multi-user data model     | `users` has real PK + unique `username`; every `secrets` and `api_tokens` row carries `user_id` FK with `ON DELETE CASCADE`. All authenticated reads/writes scope by the caller's user_id. Lets A (single-user) -> B (CLI-provisioned small group) -> C (open signup) be incremental, not a rewrite. |
| 16| Owner vs. user boundary   | The "owner" is whoever has shell access (CLI). Public signup (future) only ever creates regular users. Prevents the "first-signup-becomes-admin" race seen on Gitea et al. |
| 17| Sender-initiated cancel   | `POST /api/secrets/{id}/cancel` revokes a still-live secret: wipes the ciphertext/key/passphrase like `burn`, tags status `'canceled'` for audit, URL returns 404 thereafter. Two-click-to-confirm in the UI to prevent accidents. |
| 18| Two-click confirm pattern | All irreversible destructive UI actions (cancel a secret, clear past entries) use the same inline "arm then execute" pattern: first click tints the control red and relabels to "confirm?" for 3 s, second click within the window executes. No modals; consistent across the app. |

---

## Roles

- **Sender**: Single user. Creates secrets via web form at `/send`.
- **Receiver**: Anyone with the link. Sees an explanation page, clicks to reveal,
  secret is destroyed immediately after.

---

## Client-Side State

The app has three distinct data locations. Knowing which is which is how you
reason about consistency bugs, device-boundary behaviour, and what survives
a DB wipe.

| Where | What | Why it lives here |
|---|---|---|
| SQLite on the server | Users, secrets, API tokens, labels, tracked-status timestamps | Authoritative; survives restarts; the only source of truth that multiple browsers can share |
| `localStorage` on the sender's browser | Theme choice (`ephemera_theme_v1`), URL cache (`ephemera_urls_v1`: `{id: url}`) | Either per-device preference, or data the server cannot hold without breaking the zero-knowledge property |
| In-memory on the client | Tracked-list render state, copy-flash timers, polling interval handle | Ephemeral UI state; lost on reload, which is fine |

### Why the URL cache is client-side only

The URL returned from `POST /api/secrets` looks like `/s/{token}#{client_half}`.
The `#fragment` is the client half of the Fernet key and **never hits the
server**. That's the whole point of key splitting (see Security Design). So
if a sender wants to re-copy the URL from the tracked list later, we cannot
rebuild it server-side -- we have to cache it in the browser that created it.

We cache the URL under the server-issued UUID `id` (stable, unique, present
in every `/api/secrets/tracked` item). On render we join:
- item in server list + URL in localStorage -> clickable row
- item in server list, no URL locally -> "created elsewhere" hint, removable
- URL locally, not in server list -> silently garbage-collected

This keeps the server authoritative (labels, statuses, who's tracked) while
not leaking key material.

### Tracked-list refresh

The tracked list polls `GET /api/secrets/tracked` every 5 s while at least
one row is `pending`. Per tick:

1. Fetch; on network failure, return `null` and leave UI + URL cache alone
   (transient errors never trigger destructive cleanup).
2. Diff the ids and per-row status against the rendered DOM. If identical,
   do nothing. Otherwise, re-render (unless a `data-busy="1"` attribute is
   present on any row -- that's the 1.5 s copy-flash animation, which we
   don't interrupt).
3. When no rows remain `pending`, stop the interval. It restarts the next
   time a new pending tracked row appears (create, or renewed polling after
   the list is rebuilt).

Polling is the right fit here because:
- The load is trivial: single-user, one indexed `SELECT`, ~12 req/min peak
  per open tab. SQLite handles this without thinking about it.
- The data is low-frequency -- most tracked secrets sit in `pending` until
  someone reveals them, which for a sharing tool is minutes to hours.
- Simplicity and testability beat the ceremony of SSE / WebSockets at this
  scale.

If ephemera ever grew to many concurrent users or a more real-time UX (e.g.
showing "the receiver opened the link just now" within a second), the right
next step would be server-sent events: HTTP-streaming, one-way, cheap on the
server, same security model as normal endpoints.

### Theme

`theme.js` loads in `<head>` (before body render) so `data-theme` is set on
`<html>` before paint -- no flash of wrong theme. First-time resolution is
`prefers-color-scheme`; user clicks on the toggle persist `light` or `dark`
to `localStorage`. System-preference changes are followed only until the
user expresses an explicit choice, at which point their pick wins.

---

## UI Design

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
- **Click-to-zoom**: clicking the image (or activating via Enter/Space when
  focused) opens a fullscreen overlay at `95vw × 95vh` over an 88%-opacity
  backdrop. Click anywhere, click the top-right `close` pill, or press Escape
  to dismiss. The overlay is `role="dialog" aria-modal="true"`, focus moves to
  the close button on open and back to the thumbnail on close, and body scroll
  is locked while open. The underlying file is always the full-resolution
  original; the in-card render is the downscaled thumbnail.

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

**Caddyfile** at `/etc/caddy/Caddyfile`:

```
your-domain.com {
    reverse_proxy 127.0.0.1:8000
    request_body {
        max_size 11MB         # >10MB image cap to absorb multipart framing overhead
    }
    encode gzip zstd
    log {
        output file /var/log/caddy/ephemera.log {
            roll_size 10mb    # rotate at 10MB
            roll_keep 10      # keep the last 10 rotated files
            roll_keep_for 720h # ~30 days
        }
        format json
    }
}
```

That's it. Caddy handles TLS certificate provisioning, renewal, and the
HTTP->HTTPS redirect automatically. No certbot, no cron, no manual cert
paths. Note that Caddy does *not* add the `Strict-Transport-Security`
header on its own -- HSTS is set by the app's security-header middleware
in `app/__init__.py`.

**DNS must be set up before Caddy first starts.** Caddy requests its certificate
from Let's Encrypt via the ACME HTTP-01 challenge on first launch; if the
hostname doesn't resolve to this host yet, the challenge fails. Let's Encrypt
rate-limits repeated failures (5 duplicate-cert attempts per week), so getting
DNS correct first is worth the extra minute.

**Why one Uvicorn worker**: The "2 * CPU + 1" formula is a Gunicorn heuristic
for CPU-bound synchronous WSGI apps -- it doesn't apply here. Uvicorn is async:
a single worker handles I/O concurrency via the event loop, so it can serve
many concurrent requests without spawning extra processes. This app is I/O-bound
(SQLite reads, network), not CPU-bound. Additionally, multiple workers means
multiple OS processes, which means contention on SQLite's process-level write
lock. One worker avoids that entirely. On a 1 vCPU droplet with low-volume
personal use, one worker is the correct choice.

**systemd unit** at `/etc/systemd/system/ephemera.service`:

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
ExecStart=/opt/ephemera/venv/bin/uvicorn app:create_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
LockPersonality=true
RestrictSUIDSGID=true
ReadWritePaths=/var/lib/ephemera

[Install]
WantedBy=multi-user.target
```

Three flags in `ExecStart` are load-bearing and easy to miss:

- `--factory` -- `create_app()` is a factory function, not a module-level ASGI
  app instance. Without this flag Uvicorn tries to call `create_app.__call__`
  and fails.
- `--proxy-headers` -- tells Uvicorn to read `X-Forwarded-For` and
  `X-Forwarded-Proto` from the upstream reverse proxy and populate
  `request.client.host` / scheme accordingly.
- `--forwarded-allow-ips 127.0.0.1` -- Uvicorn only honours proxy headers from
  trusted IPs; the loopback address is correct here because Caddy runs on the
  same host. **Without both of these flags the in-memory rate limiter sees
  every request as coming from 127.0.0.1 (Caddy) and throttles all users as
  one bucket.**

The hardening stanza is optional but cheap. Relevant pieces:
- `ProtectSystem=strict` + `ReadWritePaths=/var/lib/ephemera` makes the whole
  filesystem read-only to the service except for its DB directory.
- `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectKernel*`: standard
  reductions to what a compromised service could reach.

**File locations** (all created at install time):

| Path | Owner / mode | Purpose |
|---|---|---|
| `/opt/ephemera/` | `ephemera:ephemera` | app code + `venv/` |
| `/var/lib/ephemera/` | `ephemera:ephemera` 0750 | SQLite DB + WAL/SHM sidecars |
| `/etc/ephemera/env` | `root:ephemera` **0640** | secrets (`EPHEMERA_SECRET_KEY`, etc.). Locked-down perms so only root or the service group can read it. |
| `/etc/systemd/system/ephemera.service` | `root:root` 0644 | systemd unit |
| `/etc/caddy/Caddyfile` | `root:root` 0644 | reverse proxy config |
| `/var/log/caddy/` | `caddy:caddy` | Caddy access + error logs |

### Operations

**Deploy a new version:**

```bash
cd /opt/ephemera
sudo -u ephemera git pull
sudo -u ephemera ./venv/bin/pip install -r requirements.txt
sudo systemctl restart ephemera
```

In-memory rate-limiter counters reset on restart -- acceptable for this scale.

**Logs:**

```bash
sudo journalctl -u ephemera -f     # app
sudo journalctl -u caddy -f        # TLS + HTTP pipeline
sudo tail -f /var/log/caddy/ephemera.log   # access log (JSON)
```

**Backup:** SQLite in WAL mode is safe to back up live via the atomic `.backup`
command -- don't just `cp` the db file, the WAL can make the copy inconsistent.

```bash
sudo -u ephemera /usr/bin/sqlite3 /var/lib/ephemera/ephemera.db \
  ".backup '/var/lib/ephemera/backup-$(date +%F).db'"
```

Also back up `/etc/ephemera/env`. If the `SECRET_KEY` is lost, all existing
session cookies and recovery-code hashes stay valid, but the server won't be
able to verify sessions signed with the old key -- users will just re-login.
