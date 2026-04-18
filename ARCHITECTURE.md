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
