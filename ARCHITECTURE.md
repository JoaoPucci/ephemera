# Architecture

A self-hosted one-time secret (OTS) sharing system. Secrets (text or images)
are encrypted at rest, viewable exactly once, and destroyed after viewing or
expiry.

This file is the **index** -- one paragraph of overview, the cross-cutting
decisions log, and links to the focused sub-docs under [`docs/`](docs/).
Open the doc that matches your question:

| Doc | What it covers |
|---|---|
| [`docs/requirements.md`](docs/requirements.md) | What ephemera does. Features, user flow, expiry presets, design philosophy, explicit non-features. |
| [`docs/backend.md`](docs/backend.md) | Server architecture: tech stack, core-flow sequence diagrams, security design (key splitting, passphrase, auth), DB schema, API surface. |
| [`docs/frontend.md`](docs/frontend.md) | Browser architecture: client-side state, theme system, UI design catalogue, page-by-page layouts, responsive behavior. |
| [`docs/deployment.md`](docs/deployment.md) | Public deployment recipe: Caddyfile, systemd unit, file locations, operations, rollback. |
| [`PROPOSAL-end-to-end-encryption.md`](PROPOSAL-end-to-end-encryption.md) | Where ephemera is heading: a proposal to move encryption into the browser. Feedback wanted on GitHub. |

---

## Decisions log

Cross-cutting decisions that shape multiple docs. Kept here because a
reader hunting for "why did we do X again" usually knows the choice
number or the rough chronology, not which sub-doc to look in.

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

- **Sender**: authenticated user of the web form at `/send`. Creates
  secrets, tracks them, cancels them. Users are provisioned via the admin
  CLI; there is no public signup.
- **Receiver**: anyone with the link. Unauthenticated -- the URL itself is
  the authorization. Sees an explanation page, a passphrase prompt if one
  was set, and the one-shot content after clicking reveal.

See [`docs/requirements.md`](docs/requirements.md) for the full user flow.

---

## Implementation order (historical)

The phased build plan followed during initial development. Kept as a record
of how the system came together, not a current to-do list.

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
