# ephemera — dev quick start

One-time secret sharing. See `arch_doc.md` for the full design.

## Setup (once)

```bash
cd /media/shiroyasha/linux-data/git/ephemera
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env and set EPHEMERA_API_KEY and EPHEMERA_SECRET_KEY to real random values:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Run the dev server

```bash
./venv/bin/python run.py
```

The app starts at **http://127.0.0.1:8000** with `--reload` enabled.

## Smoke test in a browser

1. Open **http://127.0.0.1:8000/send** — you get the sign-in page.
2. Paste the `EPHEMERA_API_KEY` value from `.env`, click **Sign in**.
3. You land on the create form. Type a message, pick an expiry, (optional)
   passphrase, submit. You get back a URL with a `#fragment`.
4. Open that URL in a **private/incognito** window (or a different browser) to
   simulate the receiver. You see the "Someone shared a secret with you" page.
5. Click **Reveal Secret** — the message is shown and the secret is destroyed.
6. Reload the page — it now says "This secret is no longer available."

For images: switch to the **Image** tab, drop a PNG/JPEG/GIF/WebP (10 MB max),
same flow. SVG is rejected.

## Run the test suite

```bash
./venv/bin/pytest -q
```

All tests isolate their own SQLite DB via the `tmp_db_path` fixture and reset
the in-memory rate limiter between runs. Expect 78 passing.

## Quick API smoke from the command line

```bash
# create
curl -sS -X POST http://127.0.0.1:8000/api/secrets \
  -H "Authorization: Bearer $EPHEMERA_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Origin: http://localhost:8000" \
  -d '{"content":"hi","content_type":"text","expires_in":300}'
# → {"url":"http://localhost:8000/s/<token>#<frag>", "id":"...", "expires_at":"..."}

# meta (should say passphrase_required: false)
curl -sS http://127.0.0.1:8000/s/<token>/meta

# reveal
curl -sS -X POST http://127.0.0.1:8000/s/<token>/reveal \
  -H "Content-Type: application/json" \
  -H "Origin: http://localhost:8000" \
  -d '{"key":"<frag>"}'
```

## Things to try

| Scenario | How |
|---|---|
| Passphrase-protected secret | Set a passphrase when creating; receiver must enter it before reveal |
| Burn after 5 wrong passphrases | Submit 5 wrong passphrases; secret is permanently burned (410) |
| Tracked secret | Tick "Track viewing status"; then `GET /api/secrets/{id}/status` |
| Expired secret | Set `expires_in: 300` and wait 5 minutes; background cleanup purges it |
| Cross-origin CSRF block | Send a reveal POST with `Origin: https://example.com` → 403 |
| Rate limit | More than 10 reveal attempts per minute per IP → 429 |
| Image upload | Switch to Image tab, drop a PNG/JPEG/GIF/WebP; SVG is rejected |

## Files of interest

- `app/crypto.py` — Fernet + key splitting
- `app/validation.py` — MIME + magic-byte check
- `app/models.py` — SQLite data layer
- `app/routes/sender.py` — `/send`, `/send/login`, `/api/secrets`, status
- `app/routes/receiver.py` — `/s/{token}`, `/s/{token}/meta`, `/s/{token}/reveal`
- `app/dependencies.py` — bearer auth, session cookie, origin check
- `app/limiter.py` — in-memory sliding-window rate limiter
- `app/cleanup.py` — background task purging expired + old tracked rows
- `app/static/` — plain HTML/CSS/JS frontend, no build step
