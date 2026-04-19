# Deployment

How ephemera is intended to run in production. A generic self-hostable
recipe with placeholders (`<domain>`, `<host>`), not instance specifics.
Operators typically keep their own runbook alongside this (see the
`DEPLOYMENT.md` pattern mentioned at the end) for commands tailored to
their server.

Related: [`requirements.md`](requirements.md) for *what* ephemera does,
[`backend.md`](backend.md) for the server architecture, and
[`frontend.md`](frontend.md) for the browser side.

## Architecture

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

## Caddyfile

At `/etc/caddy/Caddyfile`:

```
<domain> {
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

**DNS must be set up before Caddy first starts.** Caddy requests its
certificate from Let's Encrypt via the ACME HTTP-01 challenge on first
launch; if the hostname doesn't resolve to this host yet, the challenge
fails. Let's Encrypt rate-limits repeated failures (5 duplicate-cert
attempts per week), so getting DNS correct first is worth the extra
minute.

## Why one Uvicorn worker

The "2 * CPU + 1" formula is a Gunicorn heuristic for CPU-bound synchronous
WSGI apps -- it doesn't apply here. Uvicorn is async: a single worker
handles I/O concurrency via the event loop, so it can serve many concurrent
requests without spawning extra processes. This app is I/O-bound (SQLite
reads, network), not CPU-bound. Additionally, multiple workers means
multiple OS processes, which means contention on SQLite's process-level
write lock. One worker avoids that entirely. On a small VPS with
low-volume personal use, one worker is the correct choice.

## systemd unit

At `/etc/systemd/system/ephemera.service`:

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

## File locations

All created at install time:

| Path | Owner / mode | Purpose |
|---|---|---|
| `/opt/ephemera/` | `ephemera:ephemera` | app code + `venv/` (a working tree pinned to a release tag) |
| `/var/lib/ephemera/` | `ephemera:ephemera` 0750 | SQLite DB + WAL/SHM sidecars |
| `/etc/ephemera/env` | `root:ephemera` **0640** | secrets (`EPHEMERA_SECRET_KEY`, etc.). Locked-down perms so only root or the service group can read it. |
| `/etc/systemd/system/ephemera.service` | `root:root` 0644 | systemd unit |
| `/etc/caddy/Caddyfile` | `root:root` 0644 | reverse proxy config |
| `/var/log/caddy/` | `caddy:caddy` | Caddy access + error logs (rotated via Caddy's `roll_size`) |

## Operations

**Deploy a new version** (tag-pinned, not branch-pinned):

```bash
sudo -u ephemera git -C /opt/ephemera fetch --tags
sudo -u ephemera git -C /opt/ephemera checkout vX.Y.Z
sudo -u ephemera /opt/ephemera/venv/bin/pip install \
  --require-hashes -r /opt/ephemera/requirements.txt
sudo systemctl restart ephemera
```

`--require-hashes` makes pip verify every wheel against the SHA-256 hashes
committed in `requirements.txt` (generated via `pip-compile --generate-hashes`).
A tampered mirror, a typo-squatted package name, or an accidentally-added
unhashed dep all fail the install rather than silently proceeding.

Note that `pip install` is additive, not declarative: if a previous install
left extra packages in the venv that are no longer in `requirements.txt`
(e.g., when a dev-only package was ever installed directly), those stay.
The way to resync a venv to exactly what the file specifies is to rebuild
it from scratch:

```bash
sudo systemctl stop ephemera
sudo -u ephemera rm -rf /opt/ephemera/venv
sudo -u ephemera python3 -m venv /opt/ephemera/venv
sudo -u ephemera /opt/ephemera/venv/bin/pip install \
  --require-hashes -r /opt/ephemera/requirements.txt
sudo systemctl start ephemera
```

Do this on the next deploy after any change that removes or adds packages
to `requirements.in`.

Rollback is identical, just a different tag:

```bash
sudo -u ephemera git -C /opt/ephemera checkout vX.Y.Z-PREVIOUS
sudo systemctl restart ephemera
```

After `checkout vX.Y.Z` the tree is in detached HEAD state. That's correct
for tag-pinned deploys. Do **not** `git pull` from there -- it will refuse.
Every deploy starts with `fetch --tags` + `checkout vNEW`.

In-memory rate-limiter counters reset on restart -- acceptable at this
scale.

**Logs:**

```bash
sudo journalctl -u ephemera -f     # app stdout/stderr
sudo journalctl -u caddy -f        # TLS + HTTP pipeline
sudo tail -f /var/log/caddy/ephemera.log   # access log (JSON)
```

**Structured security events.** `app/security_log.py` emits one JSON line per
security-relevant mutation (login success/failure, lockout, reveal, burn,
cancel, tracked-clear, api-token create/revoke, user add/remove, credential
rotations). These land interleaved with regular stdout. Common triage
filters:

```bash
# All security events from the last hour:
sudo journalctl -u ephemera --since '1 hour ago' -o cat | grep '"event":' | jq .

# Failed logins grouped by username:
sudo journalctl -u ephemera --since today -o cat \
  | grep '"event":"login.failure"' | jq -r .username | sort | uniq -c

# Who was in the service this week:
sudo journalctl -u ephemera --since '7 days ago' -o cat \
  | grep '"event":"login.success"' | jq -r '[.ts,.username,.client_ip]|@tsv'
```

**Things to never log.** Any custom middleware, debug proxy, or log-shipping
pipeline configured later MUST NOT capture the following:

- Request bodies on `POST /s/{token}/reveal` — contains the URL's client-half
  key and (if set) the receiver passphrase.
- Request bodies on `POST /api/secrets` — contains the plaintext before
  encryption.
- The `Authorization` header on any endpoint — bearer API tokens.
- The `Cookie` / `Set-Cookie` headers — signed session values.
- The `totp_secret` column from the DB, or any output of `app.admin diagnose`.

Uvicorn's default access log format omits bodies and headers, and the Caddy
JSON format shipped in the Caddyfile above is safe as-is. Changes to either
side should be reviewed against this list.

**Backup:** SQLite in WAL mode is safe to back up live via the atomic
`.backup` command -- don't just `cp` the DB file, the WAL can make the copy
inconsistent.

```bash
sudo -u ephemera /usr/bin/sqlite3 /var/lib/ephemera/ephemera.db \
  ".backup '/var/lib/ephemera/backup-$(date +%F).db'"
```

Also back up `/etc/ephemera/env`. The `EPHEMERA_SECRET_KEY` value is
load-bearing for two things: it signs session cookies, and (since F-05)
a KEK is HKDF-derived from it to encrypt the stored TOTP seeds.

If the key is rotated or lost:
- All session cookies become unverifiable → every user re-logs in. Fine.
- Every stored `totp_secret` becomes undecryptable → TOTP check fails on
  login. Recovery is: each user logs in with password + one of their
  **recovery codes** (which consume-one, bcrypt'd, unaffected by SECRET_KEY),
  then runs `python -m app.admin rotate-totp` to write a fresh seed
  encrypted under the new KEK.
- Password hashes are bcrypt, stored in the DB, unaffected.

Plan rotations accordingly — coordinate with the users before flipping
the env, and make sure every user has at least one unused recovery code
on hand first (`python -m app.admin regen-recovery-codes` will mint a
fresh set).

## Operator runbook (per-instance)

Operators typically keep a private `DEPLOYMENT.md` at the repo root,
gitignored, containing everything above plus instance-specific commands
(the actual hostname, the specific SSH user, cron entries for backups,
monitoring hooks, etc.). Recommended sections to include in such a runbook:

- First-time setup (DNS, system packages, firewall, user/dir layout, env
  file, first-user provisioning).
- Release deploy (the fetch/checkout/restart ritual above).
- User management (the admin CLI commands indexed by goal).
- Backup automation (a `cron.daily` script that calls the SQLite `.backup`).
- Log rotation verification (ls on `/var/log/caddy/`).
- Troubleshooting (service won't start, cert won't provision, account
  lockout recovery, forgotten credentials, disk full).
- Decommission (clean teardown).
- Routine monthly checklist.

Kept locally and never pushed, so instance specifics stay off a public
repo.
