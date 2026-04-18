# ephemera — dev quick start

One-time secret sharing. See `arch_doc.md` for the full design.

> **Heads up — direction in progress.** A proposal to move encryption into the
> browser (end-to-end, server cannot read plaintexts) is drafted at
> [`PROPOSAL-end-to-end-encryption.md`](PROPOSAL-end-to-end-encryption.md).
> Feedback welcome via GitHub issues before implementation starts.

## Setup (once)

```bash
cd /path/to/ephemera
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env and set EPHEMERA_SECRET_KEY to a real random value:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Provision the first user (once)

```bash
./venv/bin/python -m app.admin init <username>
```

Pick a username (`admin` is a fine default), supply a password, scan the QR
into an authenticator app (1Password, Google Authenticator, Aegis, Bitwarden,
etc.), and save the 10 one-time recovery codes that get printed. **The
recovery codes are shown only once.** Additional users can be added later with
`add-user`. For everything else the CLI can do, see
[Admin CLI reference](#admin-cli-reference) below.

## Run the dev server

```bash
./venv/bin/python run.py
```

The app starts at **http://127.0.0.1:8000** with `--reload` enabled.

## Browser smoke test

1. Open **http://127.0.0.1:8000/send** — you get the sign-in page.
2. Enter your **username**, password, and the current 6-digit code from your
   authenticator.
3. You land on the create form. Type a message, pick an expiry, (optional)
   passphrase, submit. You get back a URL with a `#fragment`.
4. Open that URL in a **private/incognito** window (or a different browser) to
   simulate the receiver. You see the "Someone shared a secret with you" page.
5. Click **Reveal Secret** — the message is shown and the secret is destroyed.
6. Reload the page → "This secret is no longer available."

For images: switch to the **Image** tab, drop a PNG/JPEG/GIF/WebP (10 MB max).

## Admin CLI reference

All commands are subcommands of `python -m app.admin`. They operate on the
SQLite DB pointed to by `EPHEMERA_DB_PATH`. The server does not need to be
running.

```bash
./venv/bin/python -m app.admin <command> [args]
```

Every command that targets a specific user accepts `--user <name>` (or `-u <name>`).
When exactly one user exists, `--user` is optional — the CLI picks them automatically.

### User management

| Command | Reauth? | What it does |
|---|---|---|
| `init <username>` | — | First-time setup. Creates the initial user with that username, a password, TOTP secret (QR + manual fallback), and 10 one-time recovery codes. Refuses to run if any user already exists. |
| `add-user <username>` | yes (as any existing user) | Provision another user. Same prompts as `init`. The re-auth requirement means shell access alone isn't enough to silently mint accounts. |
| `list-users` | — | Show id, username, and created_at for every user. |
| `remove-user <username>` | yes (as that user) | Delete a user and cascade-drop all their secrets and API tokens. Refuses if it would leave the server empty. |

### Credential rotation

All require re-auth (password + TOTP or recovery code) as the target user.

| Command | What it does |
|---|---|
| `reset-password [--user <name>]` | Change the password. |
| `rotate-totp [--user <name>]` | Generate a new TOTP secret and print a new QR. The old authenticator entry dies — rescan on every device you use. |
| `regen-recovery-codes [--user <name>]` | Invalidate the current 10 recovery codes and print 10 fresh ones. Save them before closing the terminal. |

### API tokens (for scripts / CI / automation)

Each token belongs to one user; the server scopes every call made with it to
that user. Token names are unique per user, not globally — two users can both
have a token named `cli-laptop`.

| Command | Reauth? | What it does |
|---|---|---|
| `create-token <name> [--user <u>]` | yes | Mint a new revocable API token. Plaintext (`eph_…`) is printed ONCE — save it in a password manager. Only the SHA-256 hash is stored server-side. |
| `list-tokens [--user <u>]` | — | Show that user's tokens with name, status, created / last-used. |
| `revoke-token <name> [--user <u>]` | yes | Revoke a token. Revocation is immediate; next API call with it fails. |

Use a token with `Authorization: Bearer <token>` on `/api/secrets` and
`/api/secrets/{id}/status`:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/secrets \
  -H "Authorization: Bearer eph_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -H "Origin: http://127.0.0.1:8000" \
  -d '{"content":"hi","content_type":"text","expires_in":300}'
```

### Troubleshooting / self-debugging

These bypass the "don't reveal which factor is wrong" rule because at the CLI
you already have shell access — helpfulness beats ceremony.

| Command | What it does |
|---|---|
| `diagnose [--user <name>]` | Print server time, current TOTP step, the stored `totp_last_step`, and the 3 codes that would currently be accepted (previous / current / next step). Compare with what your authenticator is showing. |
| `verify [--user <name>]` | Prompt for password + code and print `OK` / `MISMATCH` for each factor independently. Does NOT mutate `totp_last_step`, so you can run it repeatedly. |

### Common scenarios

| Situation | What to do |
|---|---|
| I want to add someone to my instance | `add-user <their-username>` — re-authenticates you first, then prompts them (or you) for a password and shows a QR + recovery codes. They log in at `/send` with their own username. |
| I want to remove a user | `remove-user <username>` — requires password + TOTP of **that user** to confirm. Cascade-drops their secrets and tokens. Refuses if they're the last user. |
| I forgot my password but have my TOTP / recovery code | No path for this — `reset-password` requires the current password. Wipe just that user: `sqlite3 ephemera.db "DELETE FROM users WHERE username='<name>';"` then `add-user <name>` (or `init <name>` if they were the only user). Foreign-key cascades drop their secrets + tokens. |
| I lost my authenticator | Log in with a recovery code (login page → "Use a recovery code"). Once in, `rotate-totp` for a fresh QR and `regen-recovery-codes` to top up codes. |
| I lost everything (password, TOTP, and recovery codes) | Same as "forgot password" — nuclear option. Wipe that user and re-provision, or `rm -f ephemera.db* && python -m app.admin init <username>` for a completely fresh server. |
| My TOTP code keeps being rejected | `diagnose` → compare to authenticator. If the "current step" code doesn't match your authenticator, you have a stale entry; delete it and rescan from the last `init` / `rotate-totp` output — or just `rotate-totp` for a fresh secret. |
| Account locked (423 at login) | 10 failed attempts trigger a 1-hour lockout. Wait it out, or in dev: `sqlite3 ephemera.db "UPDATE users SET lockout_until=NULL, failed_attempts=0 WHERE username='<name>';"`. |
| I pasted an API token in a commit / log | `revoke-token <name>` immediately, then `create-token <name>` to mint a replacement. |
| I need a fresh, empty dev setup | `rm -f ephemera.db* && python -m app.admin init admin` (wipes all users, secrets, tokens). |

## Run the test suite

Three layers, each tests a different concern:

| Layer | Tool | Count | Runtime |
|---|---|---|---|
| Backend unit + integration | pytest | 150 | ~4 min |
| Frontend handlers | Vitest + jsdom | 14 | ~0.5 s |
| End-to-end golden path | Playwright (Chromium) | 1 | ~5 s |

### Backend (pytest)

```bash
./venv/bin/pytest -q
```

Every test isolates its own SQLite DB and resets in-memory rate limiters between
runs. Runtime is dominated by bcrypt cost 12 — tests exercise the real security
configuration, not a fake one.

### Frontend unit tests (Vitest + jsdom)

```bash
npm install                  # once
npm run test:unit
npm run test:unit:watch      # re-runs on save
```

Tests load each static JS file into a jsdom DOM fixture and drive the handlers
directly. No production code refactor was needed — the IIFEs are invoked via
`new Function(...)()` against a fresh DOM per test. Coverage focuses on the
in-flight guards (double-tap blocking, label swap, error-path restore) because
those are the bugs unit tests catch best.

### End-to-end smoke test (Playwright)

```bash
npm install                        # once
npx playwright install chromium    # once, ~112 MB browser download
npm run test:e2e
npm run test:e2e:headed            # watch it run in a visible browser
```

Playwright boots a throwaway uvicorn instance on port 8765 with a scoped DB
(`tests-e2e/start.sh` wipes and re-seeds every run; `tests-e2e/seed.py`
provisions a fixed user with a known TOTP secret so the test can compute valid
codes on the fly via `otplib`). The single test walks the golden path:
login → create text secret → open the URL in a second browser context →
reveal → assert content → assert a second visit shows "gone".

### Run everything

```bash
./venv/bin/pytest && npm test
```

## Things to try

| Scenario | How |
|---|---|
| Passphrase-protected secret | Set a passphrase when creating; receiver must enter it before reveal |
| Burn after 5 wrong passphrases | Submit 5 wrong passphrases; secret is permanently burned (410) |
| Tracked secret + label | Tick "Track viewing status", add a label; view it in the Tracked list (server-backed, visible from any browser after login) |
| Expired secret | Set `expires_in: 300` and wait 5 minutes; background cleanup purges it |
| Account lockout | 10 wrong password attempts → locked 1 h (returns 423 with unlock time) |
| Cross-origin CSRF block | Send a reveal POST with `Origin: https://example.com` → 403 |
| Rate limit | More than 10 reveal or login attempts/minute/IP → 429 |
| Image upload | Switch to Image tab, drop a PNG/JPEG/GIF/WebP; SVG is rejected |
| Theme switch | Top-right pill button; persists per browser |

## Files of interest

- `app/crypto.py` — Fernet + key splitting
- `app/validation.py` — MIME + magic-byte check
- `app/models.py` — SQLite data layer (secrets, users, api_tokens)
- `app/auth.py` — password + TOTP + backup codes + lockout + API-token mint/lookup
- `app/admin.py` — CLI for provisioning and credential rotation
- `app/routes/sender.py` — `/send`, `/send/login`, `/send/logout`, `/api/secrets`, status
- `app/routes/receiver.py` — `/s/{token}`, `/s/{token}/meta`, `/s/{token}/reveal`
- `app/dependencies.py` — bearer/session auth, session cookie, origin check
- `app/limiter.py` — in-memory sliding-window rate limiters (login, create, reveal)
- `app/cleanup.py` — background task purging expired + old tracked rows
- `app/static/` — plain HTML/CSS/JS frontend with light/dark theme
