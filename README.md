# ephemera

[![CI](https://github.com/JoaoPucci/ephemera/actions/workflows/ci.yml/badge.svg)](https://github.com/JoaoPucci/ephemera/actions/workflows/ci.yml)

One-time secret sharing, built from scratch.

I wanted to send OTS without handing plaintexts to someone else's server —
so I wrote ephemera and host my own. You can read the code and run your
own too. Later I'll also open my instance as a hosted service — and once
the [end-to-end encryption proposal](docs/proposals/end-to-end-encryption.md)
lands, you won't have to trust me to use it either.

Not a public service yet — there's no self-signup today, so my instance
stays personal. That opens up once self-signup is built.

## Docs

Start with whichever matches your question:

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — index: overview, cross-cutting decisions log, links to the rest.
- [`docs/requirements.md`](docs/requirements.md) — what ephemera *does*: features, user flow, explicit non-features.
- [`docs/backend.md`](docs/backend.md) — server side: tech stack, crypto, DB schema, API surface.
- [`docs/frontend.md`](docs/frontend.md) — browser side: state, UI design, responsive behavior.
- [`docs/deployment.md`](docs/deployment.md) — how to host it: Caddy + systemd recipe, operations, rollback.

> **Directions in progress — feedback welcome.**
> Two proposals are drafted for early feedback, before any code lands:
> - [`docs/proposals/end-to-end-encryption.md`](docs/proposals/end-to-end-encryption.md) — move encryption into the browser so the operator cannot read plaintexts.
> - [`docs/proposals/admin-panel.md`](docs/proposals/admin-panel.md) — explicit admin role + `/admin` page + audited destructive actions, instead of today's "shell-only admin" model.
> Open a GitHub issue on either before Phase 0 decisions are locked in.

## Security & quality

**Why this section is here.** AI-assisted builds invite legitimate skepticism
about security and rigor. This isn't marketing — it's a concrete list of the
controls that shipped, each backed by code you can read and tests you can run
yourself.

<details>
<summary><b>Concerns a secret-sharing tool has to address, and what's in place</b> — 12 items</summary>

| Concern | What's in place |
|---|---|
| DB breach exposing plaintext | Fernet + key splitting: the URL fragment (half the encryption key) never reaches the server. A database dump alone cannot decrypt anything. |
| Plaintext in logs | Uvicorn access log excludes request bodies; FastAPI exception handlers scrub sensitive data. |
| Weak authentication | bcrypt cost 12, TOTP with ±1-step tolerance and anti-replay, 10 one-time recovery codes, per-user lockout (10 failures in 15 min → 1 h). |
| Username enumeration | Constant-time bcrypt check even when the username doesn't exist. |
| Session hijacking / fixation | `HttpOnly` + `SameSite=Strict` + `Secure` cookie; session value rotated on every successful login. |
| CSRF | `Origin` header validated on every state-changing POST. Cross-origin returns 403. |
| XSS via uploads | SVG explicitly rejected; PNG / JPEG / GIF / WebP whitelist verified by magic bytes, not by the `Content-Type` header. 10 MB cap at the app; 11 MB cap at Caddy. |
| Clickjacking, MIME sniffing, referrer leaks | `Content-Security-Policy: default-src 'self'` (no inline scripts); `X-Frame-Options: DENY`; `X-Content-Type-Options: nosniff`; `Referrer-Policy: no-referrer`; `Strict-Transport-Security` (conservative first-rollout max-age, to be bumped after a cert renewal). |
| Brute force | 10/min/IP on login and reveal; 60/hr/session on create. Sliding-window, in-memory. |
| Brute force on a leaked URL | Five wrong passphrase attempts permanently burns the secret. |
| Stale / orphan data | Background cleanup purges expired rows every 60 s; tracked metadata auto-expires at 30 days; secrets hard-deleted on reveal. |
| Service compromise | systemd sandbox: `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectKernel*`, `NoNewPrivileges`. Unprivileged service user with no shell; env file at `0640 root:ephemera`. |

</details>

<details>
<summary><b>Test coverage</b> — backend, frontend, and end-to-end layers</summary>

| Layer | Tool |
|---|---|
| Backend unit + integration | pytest |
| Frontend handlers (in-flight guards, error paths) | Vitest + jsdom |
| End-to-end golden path in a real browser | Playwright (Chromium) |

Tests run against the real bcrypt cost (12) — no mocked faster config — which
is why the suite takes a few minutes. The E2E test drives a real browser
through login → create → reveal across two separate browser contexts,
exercising the full pipeline including the browser's fragment handling. For
exact counts and runtimes, run the suites (see below).

</details>

**Verify for yourself.** Every control above has a counterpart in
[`tests/`](tests/) or [`tests-js/`](tests-js/). Security design details live
in [`docs/backend.md`](docs/backend.md) (crypto, auth, hardening); hosting
and rollback in [`docs/deployment.md`](docs/deployment.md). Every release is
tagged; rollback is one command.

**Honest caveat.** The operator still serves the JavaScript that the browser
executes, so an active malicious operator could in principle swap in code
that exfiltrates plaintext before encryption. Closing that gap is the point
of the [end-to-end encryption proposal](docs/proposals/end-to-end-encryption.md) —
feedback welcome.

## Setup (once)

```bash
cd /path/to/ephemera
python3 -m venv venv
./venv/bin/pip install --require-hashes -r requirements-dev.txt   # runtime + test deps
# or `-r requirements.txt` for a runtime-only install (what the server uses)
# To add / bump a dep: edit requirements.in or requirements-dev.in, then run
#   ./venv/bin/pip-compile --generate-hashes --resolver=backtracking requirements.in
#   ./venv/bin/pip-compile --generate-hashes --resolver=backtracking --allow-unsafe requirements-dev.in
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

## Support

If ephemera is useful to you, you're welcome to chip in. It's less about
this project's hosting bill than about the freedom to keep building things
like it — new projects, same spirit, on my own time and on my own terms.
Appreciated but never expected; the project keeps going either way.

ETH: `0x097cD53Dc5Dda28c4f6A4431EA014916891beC02`

## Admin CLI reference

<details>
<summary>All CLI commands grouped by purpose — user management, credential rotation, API tokens, troubleshooting, common scenarios</summary>

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
| `remove-user <username> [--force]` | yes — as `<username>` (normal) or as any other user (with `--force`) | Delete a user and cascade-drop all their secrets and API tokens. Refuses if it would leave the server empty. `--force` is the escape hatch for deleting someone whose credentials you no longer have — you re-auth as any other user instead. Requires at least two users. |

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
| I want to remove a user (I have their creds) | `remove-user <username>` — requires password + TOTP of **that user** to confirm. Cascade-drops their secrets and tokens. Refuses if they're the last user. |
| I want to remove a user but I've lost their creds | `remove-user <username> --force` — re-auths as any other user with a valid account. Same cascade and "refuses if last user" rules. |
| I forgot my password but have my TOTP / recovery code | No path for this — `reset-password` requires the current password. Wipe just that user: `sqlite3 ephemera.db "DELETE FROM users WHERE username='<name>';"` then `add-user <name>` (or `init <name>` if they were the only user). Foreign-key cascades drop their secrets + tokens. |
| I lost my authenticator | Log in with a recovery code (login page → "Use a recovery code"). Once in, `rotate-totp` for a fresh QR and `regen-recovery-codes` to top up codes. |
| I lost everything (password, TOTP, and recovery codes) | Same as "forgot password" — nuclear option. Wipe that user and re-provision, or `rm -f ephemera.db* && python -m app.admin init <username>` for a completely fresh server. |
| My TOTP code keeps being rejected | `diagnose` → compare to authenticator. If the "current step" code doesn't match your authenticator, you have a stale entry; delete it and rescan from the last `init` / `rotate-totp` output — or just `rotate-totp` for a fresh secret. |
| Account locked (423 at login) | 10 failed attempts trigger a 1-hour lockout. Wait it out, or in dev: `sqlite3 ephemera.db "UPDATE users SET lockout_until=NULL, failed_attempts=0 WHERE username='<name>';"`. |
| I pasted an API token in a commit / log | `revoke-token <name>` immediately, then `create-token <name>` to mint a replacement. |
| I need a fresh, empty dev setup | `rm -f ephemera.db* && python -m app.admin init admin` (wipes all users, secrets, tokens). |

</details>

## Run the test suite

<details>
<summary>Three layers — pytest (backend), Vitest + jsdom (frontend), Playwright (E2E). Commands and what each covers.</summary>

Three layers, each tests a different concern:

| Layer | Tool | Rough runtime |
|---|---|---|
| Backend unit + integration | pytest | minutes (dominated by bcrypt cost 12) |
| Frontend handlers | Vitest + jsdom | under a second |
| End-to-end golden path | Playwright (Chromium) | a few seconds + server boot |

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

</details>

## Things to try

<details>
<summary>Nine scenarios exercising passphrase, tracking, expiry, lockout, rate limits, uploads, theming</summary>

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

</details>

## Files of interest

<details>
<summary>Key source files, one-liner each</summary>

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

</details>
