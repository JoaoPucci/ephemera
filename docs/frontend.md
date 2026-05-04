# Frontend Architecture

The browser side of ephemera: where state lives, how the UI is built, the
theme and responsive story, and the page-by-page layout. Server
architecture is in [`backend.md`](backend.md); product-level intent is in
[`requirements.md`](requirements.md); the index is in
[`../ARCHITECTURE.md`](../ARCHITECTURE.md).

## Tech stack

| Component        | Choice                        | Rationale                                      |
|------------------|-------------------------------|-------------------------------------------------|
| Markup           | Plain HTML (static files)     | No build step, no framework, cached cheaply    |
| Styling          | CSS custom properties + `color-mix()` | One sheet, two themes via `[data-theme]` |
| Scripting        | Vanilla JS (ES2022+)          | No bundler, no npm runtime deps, readable as-is |
| Crypto (browser) | Native WebCrypto (receive-side image rendering only; keys live in URL fragment today) | Zero added bytes; see the [E2E proposal](proposals/end-to-end-encryption.md) for where this grows |
| Testing (unit)   | Vitest + jsdom                | IIFE scripts evaluated in a DOM fixture per test |
| Testing (E2E)    | Playwright (Chromium)         | Real browser, real crypto, one golden-path test |

No bundler. No preprocessor. No frontend framework. Bundle size is whatever
the raw files weigh. Every script loads from the same origin (CSP is
`script-src 'self'`). The pages are split into small focused modules
(theme, i18n shim, copy helper, chrome menu, mask toggle, two-click
confirm) plus per-page entry points; the chrome scripts are shared
across pages, the per-page entries are loaded only where needed.
That choice is the main reason the UI feels fast on mobile over a cold
connection -- there's nothing to hydrate.

## Client-side state

The app has three distinct data locations. Knowing which is which is how you
reason about consistency bugs, device-boundary behaviour, and what survives
a DB wipe.

| Where | What | Why it lives here |
|---|---|---|
| SQLite on the server | Users, secrets, API tokens, labels, tracked-status timestamps | Authoritative; survives restarts; the only source of truth that multiple browsers can share |
| `localStorage` on the sender's browser | Theme choice (`ephemera_theme_v1`), language preference (`ephemera_lang_v1`, mirrored in a cookie so the server-side resolver sees it pre-auth), URL cache (`ephemera_urls_v1`: `{id: url}`) | Either per-device preference, or data the server cannot hold without breaking the zero-knowledge property |
| In-memory on the client | Tracked-list render state, copy-flash timers, polling interval handle | Ephemeral UI state; lost on reload, which is fine |

### Why the URL cache is client-side only

The URL returned from `POST /api/secrets` looks like `/s/{token}#{client_half}`.
The `#fragment` is the client half of the Fernet key and **never hits the
server**. That's the whole point of key splitting (see
[`backend.md`](backend.md#key-splitting-zero-knowledge-encryption)). So if a
sender wants to re-copy the URL from the tracked list later, we cannot
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

## UI design

### Visual language

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

### Page-by-page layout

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
|  |  |  Username                     |  |  |
|  |  +-------------------------------+  |  |
|  |  +-------------------------------+  |  |
|  |  |  Password            [show]   |  |  |
|  |  +-------------------------------+  |  |
|  |  +-------------------------------+  |  |
|  |  |  6-digit code                 |  |  |
|  |  +-------------------------------+  |  |
|  |                                     |  |
|  |           [ Sign in ]               |  |
|  |                                     |  |
|  |           Use a recovery code       |  |
|  +-------------------------------------+  |
|                                           |
+-------------------------------------------+
```

- Password field has an inline show/hide toggle.
- TOTP field accepts either a 6-digit code or (via the toggle below) a
  10-character recovery code.
- Wrong login returns a generic "invalid credentials" -- no leak about which
  of username/password/code is wrong.

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
- The submit button disables + re-labels "Creating…" while the request is in
  flight so a rapid double-tap doesn't create two secrets (see decision #18).

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

### Responsive behavior

- The 520px card + padding works on any screen >= 360px wide.
- On narrow screens (<480px): card loses horizontal margin and side
  border-radius, becomes full-width with top/bottom margin only.
- Body top padding bumps up on mobile so the fixed theme-toggle / user-pill
  corner controls don't collide with the wordmark.
- Tracked-list rows stack vertically under 480px (label + time on one line,
  status pill + cancel + remove buttons on the next) so nothing squishes.
- Touch targets: all buttons and inputs are at least 44px tall (iOS/Android
  accessibility minimum).
- `touch-action: manipulation` on buttons kills the 300ms tap delay on older
  mobile browsers.
- No horizontal scroll at any viewport size.

## Project structure (browser files)

Two directories carry the browser surface:

- `app/templates/` -- Jinja2 shells the server renders per request
  (chrome wrapper + locale-resolved labels + per-page page-shape).
  Lives there rather than under `static/` because it's *rendered*, not
  served as-is. Backed by the locale resolver in `app/i18n.py`; details
  in [`backend.md`](backend.md#tech-stack).
- `app/static/` -- everything served by `StaticFiles` as-is: stylesheets,
  scripts, image assets, vendored Swagger UI, JSON i18n catalogues for
  the client-side string shim.

The server side of the tree is in
[`backend.md`](backend.md#project-structure).

```
app/templates/
  _layout.html          # Chrome shell every page extends: <head>, top-chrome pills,
                        #   bottom wordmark, hamburger drawer, scripts manifest
  _docs.html            # Swagger UI shell (auth-gated) -- loads /static/swagger/* only
  login.html            # Password + TOTP / recovery-code sign-in form
  sender.html           # Secret-creation form + tracked-list section
  landing.html          # Receiver: explanation, reveal button, "gone" / "burned" /
                        #   "expired" branches; JS toggles passphrase UI from /meta

app/static/
  # ---- Design tokens + base + chrome (split-by-concern stylesheets) ----
  tokens.css            # CSS custom properties: palette, spacing, type, motion;
                        #   the [data-theme="dark"] override block is here too
  base.css              # Reset, typography baseline, layout primitives
  forms.css             # Input + button + tab + dropdown + global :hover / :focus
  components.css        # Reusable UI components: cards, pills, switches, popovers
  chrome.css            # Top-chrome pills (desktop) + mobile hamburger drawer
                        #   shape; the 720px breakpoint that flips between them
  responsive.css        # Viewport-specific overrides above the chrome split

  # ---- Per-feature JS modules; loaded by template-level <script> tags ----
  theme.js              # Persists `ephemera_theme_v1`; loaded in <head> so
                        #   data-theme is set on <html> before paint (no flash)
  i18n.js               # t(key, vars) shim + language-picker change handler
                        #   (writes cookie + localStorage, fires PATCH
                        #    /api/me/language, reloads to repaint chrome)
  lang-confirm.js       # Intercepts a picker change while the form is dirty
                        #   (typed content / attached image) so a reload doesn't
                        #   silently destroy the user's draft
  chrome-menu.js        # Mobile drawer: open / close, focus trap, scrim tap
  copy.js               # Shared copy-to-clipboard with label-swap feedback
  mask-toggle.js        # show/hide affordance for password + passphrase fields
  two-click.js          # Two-step confirm wrapper for destructive actions
                        #   (clear-history, untrack)
  analytics-toggle.js   # Analytics-opt-in switch in the chrome-menu;
                        #   PATCHes /api/me/preferences and updates the pill
  login.js              # Login submit, in-flight guard, error-state restore
  reveal.js             # Calls /meta, reads URL fragment, sends reveal POST,
                        #   paints the JSON response (text or image) into the DOM
  sender.js             # Sender-page entry: bootstraps the modules below

  sender/               # Sender form sub-package (split when the file
                        #   crossed the readability threshold)
    form.js             # Compose-form orchestration: submit, tab toggle,
                        #   "create another" reset, /api/me opt-in gate
    dropzone.js         # Image-tab click + drag-drop + paste wiring
    hints.js            # Char-counter / paste-warning / near-cap hints
    status-poll.js      # Live status pill for the just-created secret
    tracked-list.js     # Tracked-secrets render + 5s polling loop
    url-cache.js        # `ephemera_urls_v1` localStorage cache (per the
                        #   client-side-state section above)

  # ---- Static assets ----
  i18n/                 # Per-locale JSON catalogues: en.json, ja.json,
                        #   pt-BR.json, ar.json, ... -- read by i18n.js
                        #   via the <script type="application/json"> embed
  icons/                # PWA icon set (any + maskable, light + dark, 192/512);
                        #   apple-touch-icon variants for iOS standalone install
  favicon-light.svg     # Theme-aware favicons (linked from _layout.html)
  favicon-dark.svg
  swagger/              # Vendored Swagger UI bundle (pinned versions);
                        #   served behind /docs and /openapi.json (auth-gated)
```

The split-by-concern stylesheets and the per-feature JS modules are deliberate:
each file owns one concern, the dependency graph between them is shallow, and a
bug in (say) the analytics toggle never has to read or modify code in
chrome.css. The cost of "more files" is paid once; the win is editability and
not having to rebuild a mental cache to touch any single piece.

### Tests

| Suite | Tool | What it covers |
|---|---|---|
| `tests-js/` | Vitest + jsdom | Per-module unit tests: each module run against a DOM fixture under `tests-js/fixtures/`. Covers in-flight guards, error-path button restore, success-state swap, polling cadence, the analytics opt-in PATCH wiring, the language-picker dirty-form guard, etc. The fitness-functions test (`tests-js/fitness-functions.test.js`) pins JS-side architecture invariants in the same shape `tests/test_fitness_functions.py` pins them on the Python side. |
| `tests-e2e/` | Playwright (Chromium) | Acceptance suite: the system's spec layer (per [`AGENTS.md`](../AGENTS.md) §3). Covers the smoke path, image-secret creation + reveal, passphrase flow, expired-secret state, rate-limit-hit surface, mobile viewport, css-cascade regressions, and sender-side cancel. Implementation gives if e2e fails, not the other way. |

Seed and boot live in `tests-e2e/start.sh` (wipes a scoped DB, provisions a
known user with a fixed TOTP secret, then execs uvicorn on a test port with
the `EPHEMERA_E2E_TEST_HOOKS` gate enabled so the suite can reset the
in-memory limiter and force-expire secrets without sleeping through real
time -- see [`backend.md`](backend.md#_test--env-gated-e2e-only) for the
gate's posture).
