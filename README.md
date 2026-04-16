# ephemera — dev quick start

One-time secret sharing. See `arch_doc.md` for the full design.

## Setup (once)

```bash
cd /media/shiroyasha/linux-data/git/ephemera
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env and set EPHEMERA_SECRET_KEY to a real random value:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Provision the sender account (once)

```bash
./venv/bin/python -m app.admin init
```

Prompts for a password, shows a QR to scan into an authenticator app
(1Password, Google Authenticator, Aegis, Bitwarden, etc.), and prints 10
one-time recovery codes. **Save the recovery codes somewhere safe — they are
shown only once.** For everything else the CLI can do, see
[Admin CLI reference](#admin-cli-reference) below.

## Run the dev server

```bash
./venv/bin/python run.py
```

The app starts at **http://127.0.0.1:8000** with `--reload` enabled.

## Browser smoke test

1. Open **http://127.0.0.1:8000/send** — you get the sign-in page.
2. Enter your password and the current 6-digit code from your authenticator.
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

### Provisioning

| Command | Reauth? | What it does |
|---|---|---|
| `init` | — | First-time setup. Prompts for a password (≥10 chars, confirmed twice), generates the TOTP secret, prints a terminal-rendered QR code for your authenticator, and prints 10 one-time recovery codes. Refuses to run if a user already exists. |

### Credential rotation

All require re-auth (password + TOTP or recovery code) before running.

| Command | What it does |
|---|---|
| `reset-password` | Change the password. |
| `rotate-totp` | Generate a new TOTP secret and print a new QR. The old authenticator entry becomes dead — rescan on every device you use. |
| `regen-recovery-codes` | Invalidate the current 10 recovery codes and print 10 fresh ones. Save them before closing the terminal. |

### API tokens (for scripts / CI / automation)

| Command | Reauth? | What it does |
|---|---|---|
| `create-token <name>` | yes | Mint a new revocable API token. Plaintext (`eph_…`) is printed ONCE — save it in a password manager. Only the SHA-256 hash is stored. |
| `list-tokens` | — | Show all tokens with their name, status, created/last-used timestamps. |
| `revoke-token <name>` | yes | Revoke a token by its name. Revocation is immediate; active requests using it will start failing on next call. |

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
| `diagnose` | Print server time, current TOTP step, the stored `totp_last_step`, and the 3 codes that would currently be accepted (previous / current / next step). Compare with what your authenticator is showing. |
| `verify` | Prompt for password + code and print `OK` / `MISMATCH` for each factor independently. Does NOT mutate `totp_last_step`, so you can run it repeatedly. |

### Common scenarios

| Situation | What to do |
|---|---|
| I forgot my password | No server-side recovery — wipe the user row and re-init: `sqlite3 ephemera.db "DELETE FROM users; DELETE FROM api_tokens;"` then `python -m app.admin init`. |
| I lost access to my authenticator | Log in with a recovery code (login form → "Use a recovery code"), then `rotate-totp` to generate a fresh one. If you lost recovery codes too, you're in the "forgot password" case. |
| My TOTP code keeps being rejected | `diagnose` → compare to authenticator. If the "current step" code doesn't match your authenticator, you have a stale entry; delete it and rescan from the last `init` / `rotate-totp` output — or just `rotate-totp` for a fresh secret. |
| Account locked (423 at login) | 10 failed attempts trigger a 1-hour lockout. No unlock command by design. In dev: `sqlite3 ephemera.db "UPDATE users SET lockout_until=NULL, failed_attempts=0"`. |
| I pasted an API token in a commit / log | `revoke-token <name>` immediately, then `create-token <name>` to mint a replacement. |
| I need a fresh, empty dev setup | `rm -f ephemera.db* && python -m app.admin init` (wipes secrets too). |

## Run the test suite

```bash
./venv/bin/pytest -q
```

All tests isolate their own SQLite DB and reset in-memory rate limiters between
runs. Expect 109 passing. Runtime is ~2 minutes because bcrypt cost 12 is
intentionally slow — tests exercise the real security configuration.

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
