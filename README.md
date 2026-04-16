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

Create your user (password + TOTP + recovery codes):

```bash
./venv/bin/python -m app.admin init
```

You'll be asked for a password, then shown a QR code to scan into an
authenticator app (1Password, Google Authenticator, Aegis, etc.), plus 10
one-time recovery codes. **Save the recovery codes somewhere safe — they are
shown only once.**

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

## API tokens (for scripts / CI)

Mint a revocable token:

```bash
./venv/bin/python -m app.admin create-token cli-laptop
# → Authorization: Bearer eph_xxxxx  (shown ONCE)

./venv/bin/python -m app.admin list-tokens
./venv/bin/python -m app.admin revoke-token cli-laptop
```

```bash
# Example API call:
curl -sS -X POST http://127.0.0.1:8000/api/secrets \
  -H "Authorization: Bearer eph_xxxxx" \
  -H "Content-Type: application/json" \
  -H "Origin: http://localhost:8000" \
  -d '{"content":"hi","content_type":"text","expires_in":300}'
```

## Credential rotation

```bash
./venv/bin/python -m app.admin reset-password       # requires TOTP
./venv/bin/python -m app.admin rotate-totp          # requires password
./venv/bin/python -m app.admin regen-recovery-codes # requires both
```

All rotation commands re-authenticate first, so a compromised terminal session
alone can't change credentials.

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
| Tracked secret + label | Tick "Track viewing status", add a label; view it in the Tracked list |
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
