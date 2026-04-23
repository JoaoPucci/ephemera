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
| `/etc/ephemera/env` | `root:ephemera` **0640** | secrets (`EPHEMERA_SECRET_KEY`, etc.). Locked-down perms so only root or the service group can read it. Consumed by systemd via `EnvironmentFile=` *and* auto-discovered by the admin CLI, so `sudo -u ephemera python -m app.admin …` picks up the same config as the live service without any `source`-ing. |
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
load-bearing for two things: it signs session cookies, and a KEK is
HKDF-derived from it to encrypt the stored TOTP seeds at rest.

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

## Automated deploy from GitHub Actions

Optional. The manual `fetch → checkout → pip install → restart` recipe
above stays as the break-glass fallback. Once the one-time setup below
is done, `git push origin vX.Y.Z` from the operator laptop (the `scripts/
release.sh` invocation) is enough to ship — the workflow at
`.github/workflows/deploy.yml` fires on any semver tag landing on
`origin`, joins the server's Tailscale tailnet, SSHes in as a dedicated
`deploy` user, verifies that the latest daily SQLite backup is fresh
(≤36h old) and passes `PRAGMA integrity_check`, checks out the tag,
`pip install --require-hashes`es, restarts the service, and polls
`/healthz` for up to 20 seconds before declaring success. If the latest
backup is older than 36h or fails integrity, the deploy aborts before
touching code — a broken backup posture should stop the pipeline, not
ride along silently. The daily backup cron itself is left untouched;
the deploy reads what's there rather than writing a new file.

The server-side sequence lives at `scripts/deploy/deploy.sh` (version-
controlled, readable by anyone). The trust boundary between SSH input
and the pipeline is a small root-owned entry stub installed from
`scripts/deploy/ephemera-deploy-entry`. Pre-release tags (anything with
a dash, e.g. `v1.2.3-rc1`) are deliberately excluded — the tag glob is
`'v*.*.*'` minus `'v*.*.*-*'`, and the entry stub re-validates against
`^v[0-9]+\.[0-9]+\.[0-9]+$`.

### One-time server setup

Run as root on the server.

1. **Install Tailscale.** SSH is bound to the tailnet so there is no
   public port 22 exposure at all:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up --authkey=tskey-auth-... --hostname=ephemera --ssh=false
   ```

   `--ssh=false` keeps OpenSSH as the SSH authority. Record the
   MagicDNS name the tailnet assigns (e.g. `ephemera.tail1234.ts.net`);
   it goes in `DEPLOY_SSH_HOST` below.

2. **Create the `deploy` user.** System account, bash shell so forced-
   command can run, no password, no login apart from the forced-command
   key:

   ```bash
   useradd --system --shell /bin/bash --create-home deploy
   install -o deploy -g deploy -m 0700 -d /home/deploy/.ssh
   ```

3. **Install the entry stub.** Root-owned trust boundary:

   ```bash
   install -o root -g root -m 0755 /opt/ephemera/scripts/deploy/ephemera-deploy-entry /usr/local/sbin/ephemera-deploy-entry
   ```

   Reinstall after every release (the source-of-truth lives in the
   repo; changes to it ship with the tag that contains them).

4. **Register the Actions public key** at
   `/home/deploy/.ssh/authorized_keys` (`deploy:deploy 0600`):

   ```
   restrict,command="/usr/local/sbin/ephemera-deploy-entry" ssh-ed25519 AAAA... ephemera-gha-deploy
   ```

   `restrict` disables port / agent / X11 forwarding and pty
   allocation. The key's private half lives in the repo secret
   `DEPLOY_SSH_KEY`. No `from=` clause is needed because SSH is
   already tailnet-bound.

5. **Sudoers fragment.** Validate with `visudo -c -f /etc/sudoers.d/ephemera-deploy`:

   ```
   deploy ALL=(ephemera) NOPASSWD: /opt/ephemera/scripts/deploy/deploy.sh
   deploy ALL=(root) NOPASSWD: /bin/systemctl restart ephemera, /bin/systemctl is-active ephemera
   ```

   Exact forms, no wildcards — a trailing `...` would let a future
   caller slip extra argv in.

6. **Lock SSH to the tailnet.** Remove the laptop-IP allow from UFW
   and replace it with a tailnet-interface rule:

   ```bash
   ufw delete allow from <laptop-ip> to any port 22 proto tcp
   ufw allow in on tailscale0 to any port 22 proto tcp
   ufw reload
   ```

   Operator SSH now also rides the tailnet. No functional change;
   same workflow, different route.

7. **GitHub repo secrets.** Set via `gh secret set` or the web UI:

   | Name | Value |
   |---|---|
   | `TAILSCALE_OAUTH_CLIENT_ID` | OAuth client ID for an ephemeral-auth client, scope `auth_keys`, tagged `tag:ci` |
   | `TAILSCALE_OAUTH_SECRET` | OAuth client secret paired with the ID above |
   | `DEPLOY_SSH_KEY` | ed25519 private key (PEM); public half is in `authorized_keys` at step 4 |
   | `DEPLOY_SSH_HOST` | MagicDNS name from step 1 (`ephemera.tail1234.ts.net`). Never the public DNS or IP. |
   | `DEPLOY_SSH_KNOWN_HOSTS` | Output of `ssh-keyscan ephemera.tail1234.ts.net` run from a host already on the tailnet |

8. **Tag protection rule** (GitHub → Settings → Rules → New tag
   ruleset). Pattern `v*.*.*`, restrict tag creation to repository
   admins. Blocks a compromised token from pushing a malicious tag
   that would trigger a deploy.

### Smoke-test the setup

From a host already on the tailnet (e.g. your laptop), before the
first real Actions-triggered deploy:

```bash
ssh deploy@ephemera.tail1234.ts.net v0.0.0
```

Expect `deploy entry: tag 'v0.0.0' does not match vMAJOR.MINOR.PATCH`
followed by `exit 2` — the regex is right but `v0.0.0` is a tag
that doesn't exist. Positive-path smoke test: use the currently-
deployed tag (the command is idempotent; `git checkout` on a tag
already checked out is a no-op, but `pip install` and `restart`
will still run).

### Failure & rollback

Any step failing in `deploy.sh` exits nonzero with `DEPLOY FAILED: <step>`
on stderr. The CI job fails; the service stays on whatever state the
previous successful deploy left it in (if pip aborted mid-install, the
venv may be half-updated — rebuild with the second recipe in
`## Operations` above).

No automatic rollback. Roll back manually:

```bash
sudo -u ephemera git -C /opt/ephemera checkout vX.Y.Z-PREVIOUS
sudo -u ephemera /opt/ephemera/venv/bin/pip install \
    --require-hashes -r /opt/ephemera/requirements.txt
sudo systemctl restart ephemera
```

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

---

## Translations

Ephemera's i18n stack is in place; the launched-locales list grows as
translators onboard. Today: **en**. Resolution machinery recognizes the
full `SUPPORTED` set (**en, ja, pt-BR, es, zh-CN, zh-TW**) so an
authenticated user's preference or a forced-locale share link (see
§Locale resolution below) can address any of them, but the picker
widget only renders tags listed in `LAUNCHED` — a locale with an empty
catalog would resolve cleanly and then render 100% English, which is
worse UX than not offering it.

`en` is the source of truth; every string starts life in English and
is translated from there. Two parallel catalogs, one workflow:

| Layer | Format | On-disk path | Edited by |
|---|---|---|---|
| Python + Jinja2 templates | gettext `.po` → `.mo` | `app/translations/<POSIX>/LC_MESSAGES/messages.{po,mo}` | Translator (`.po`); toolchain produces `.mo` |
| JS-rendered strings | hand-authored JSON | `app/static/i18n/<BCP47>.json` | Translator |

BCP-47 (wire format, `pt-BR`/`zh-CN`) vs POSIX (filesystem, `pt_BR`/
`zh_Hans`): the two conventions are reconciled in `POSIX_MAP` inside
`app/i18n.py`. Don't inline the conversion anywhere else — one place
to change means one place to break.

### Locale resolution

Precedence (high → low), first match wins:

1. `?lang=xx` query parameter (shareable forced-locale links; also used
   by tests). Public feature, stateless — does not write the cookie.
2. `ephemera_lang_v1` cookie (set by the picker widget).
3. `users.preferred_language` column (authenticated users; persisted
   via `PATCH /api/me/language`).
4. `Accept-Language` header.
5. `en` (default).

The cookie beating the DB preference is deliberate: when the picker
fires, it writes the cookie first and PATCHes the DB asynchronously.
If the PATCH fails (network blip, server rejects), the cookie wins so
the user still sees the locale they just picked. Normal operation
keeps cookie and DB in sync; the cookie-wins layer only matters when
they've diverged.

Unknown tags fall through silently. Locale is advisory, so a bad hint
never 400s a request.

### Error-response shape (API contract)

HTTP error responses across the app now use a structured `detail`
object rather than a bare string. The old shape (pre-i18n) returned:

```json
{ "detail": "invalid credentials" }
```

The new shape returns:

```json
{ "detail": { "code": "invalid_credentials", "message": "Invalid credentials." } }
```

`code` is a stable snake_case identifier that never changes; `message`
is the English fallback for curl / unauthenticated CLI / API clients
that don't resolve the code against a localized catalog. Extra
context fields (e.g. `until=<iso-timestamp>` on lockout responses)
merge into the object alongside `code` and `message`.

**Breaking change for API consumers** that matched on `detail` as a
string: switch to `response.json()["detail"]["code"]` for stable
programmatic matching, or read `["detail"]["message"]` for the human
text. The code vocabulary lives in `app/errors.py`; every code there
is a public contract.

### Refreshing catalogs after a string change

After editing any `_("...")` call in a `.py` or `.html` template:

```bash
./scripts/i18n.sh extract    # rescan sources -> app/translations/messages.pot
./scripts/i18n.sh update     # merge POT into every locale's .po
# translate empty msgstr entries in app/translations/<locale>/LC_MESSAGES/messages.po
./scripts/i18n.sh compile    # .po -> binary .mo
```

Commit the `.po`, `.mo`, and POT template together. Compiled `.mo`
files ship in the repo so the single-binary deploy has no build step —
a release tag contains everything the runtime needs.

Strings rendered by JavaScript live in `app/static/i18n/<locale>.json`
and are hand-authored against the keys in `en.json`. No extraction
tool; the shim in `app/static/i18n.js` looks up dotted keys like
`error.wrong_passphrase` against the active catalog and falls through
to English for any miss. The `test_every_js_i18n_key_exists_in_en_catalog`
test (in `tests/test_i18n.py`) asserts every `i18n.t('...')` call site
in the JS resolves to a real key in `en.json` — so "added a
translation call, forgot to add the key" fails CI instead of rendering
the key as a literal sentinel to users.

### Plural forms (per-locale CLDR categories)

The JS shim resolves plurals via `Intl.PluralRules(locale).select(n)`,
which returns a CLDR category from `{zero, one, two, few, many, other}`.
English uses `one` + `other`. Russian uses `one` + `few` + `many` +
`other`. Arabic uses all six.

Translators for new locales must include every category their language
uses in the JSON catalog, not just English's two. Example:

```json
"button.clear_past": {
  "one":   "...",
  "few":   "...",   // Russian, Polish, etc.
  "many":  "...",   // same
  "other": "..."    // always required -- CLDR's fallback category
}
```

If a category is missing, the shim falls back to the English
catalog's version — which uses English's plural rules, not the target
language's, and will render wrong.

### Adding a new locale

No `app/i18n.py` edit needed in the typical case — `SUPPORTED`,
`POSIX_MAP`, and `LANGUAGE_LABELS` are derived from the filesystem at
import time. Drop the catalog files on disk and the locale appears.

1. Bootstrap the gettext catalog:
   ```bash
   ./scripts/i18n.sh init <POSIX>      # e.g. fr, pt_PT
   ```
2. Create an empty JSON stub:
   ```bash
   echo '{}' > app/static/i18n/<BCP47>.json
   ```
3. Translate the `.po` msgstr entries. Populate the JSON, including
   every CLDR plural category the locale uses (see above).
4. Compile the `.mo`:
   ```bash
   ./scripts/i18n.sh compile
   ```
5. Run the suite. `test_every_js_i18n_key_exists_in_en_catalog`
   catches any JS `i18n.t()` call sites you missed in the JSON.
6. (Optional) Check the endonym the picker will render:
   ```bash
   ./venv/bin/python -c "from app.i18n import LANGUAGE_LABELS; print(LANGUAGE_LABELS['<BCP47>'])"
   ```
   The label comes from `babel.Locale.parse(<tag>).get_display_name(locale=...)`.
   If it looks wrong — verbose, mis-cased, wrong script — add an
   entry to `_LABEL_OVERRIDES` in `app/i18n.py`. Most locales won't
   need this; it's an aesthetic override, not a correctness gate.

The locale auto-joins `SUPPORTED` and the picker as soon as both the
JSON and the compiled `.mo` are in place. To ship a locale
resolution-only (reachable via `?lang=<tag>` and persisted prefs, but
hidden from the picker — useful when translations are still under
review), add the BCP-47 tag to `_LAUNCH_OPT_OUT` in `app/i18n.py`.

Half-shipped locales (JSON without `.po` or vice versa) are skipped
silently by discovery. Within a locale, any un-translated msgid
renders its English source verbatim (gettext's null-catalog
fallthrough); any missing JSON key resolves through the English
fallback catalog the template inlines into every page. Partial
translations are safe to ship.

### Deploy impact

None beyond a regular release. The `.mo` files and JSON catalogs are
tracked repo contents, picked up by the normal tag-pinned deploy flow.
If a `.mo` is ever missing on disk, the runtime silently falls back to
English rather than 500'ing — visible to users in that locale but not
service-breaking. Adding Babel and Jinja2 as runtime dependencies
means the hash-pinned `requirements.txt` grew; operators deploying
over the v0.6.x line will see the `pip install --require-hashes` step
fetch the new wheels on the next deploy.
