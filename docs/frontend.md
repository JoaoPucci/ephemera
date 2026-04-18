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
the raw files weigh. Every page loads at most three scripts, all from the
same origin. That choice is the main reason the UI feels fast on mobile over
a cold connection -- there's nothing to hydrate.

## Client-side state

The app has three distinct data locations. Knowing which is which is how you
reason about consistency bugs, device-boundary behaviour, and what survives
a DB wipe.

| Where | What | Why it lives here |
|---|---|---|
| SQLite on the server | Users, secrets, API tokens, labels, tracked-status timestamps | Authoritative; survives restarts; the only source of truth that multiple browsers can share |
| `localStorage` on the sender's browser | Theme choice (`ephemera_theme_v1`), URL cache (`ephemera_urls_v1`: `{id: url}`) | Either per-device preference, or data the server cannot hold without breaking the zero-knowledge property |
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

Lives under `app/static/`. The server side of the tree is in
[`backend.md`](backend.md#project-structure).

```
app/static/
  login.html           # Password + TOTP (or recovery) sign-in form
  sender.html          # Secret creation form; tracked-list section; logout button
  landing.html         # Receiver: explanation + reveal button; JS toggles passphrase UI
  gone.html            # Secret expired, already viewed, or burned (fallback page)
  style.css            # Design tokens + light/dark themes via [data-theme]
  theme.js             # Theme picker: persists `ephemera_theme_v1`, applied pre-render
  copy.js              # Shared copy-to-clipboard with label-swap feedback
  login.js             # Login submit, password visibility toggle, one-time-code field wipe
  sender.js            # Form submit, tab toggle, drag-drop, tracked-list render + 5s poll
  reveal.js            # Calls /meta, reads URL fragment, sends reveal POST, renders result
```

Each HTML file loads only the scripts it needs; nothing is shared by
accident. `copy.js` and `theme.js` are the two that multiple pages share.

### Tests

| Suite | Tool | What it covers |
|---|---|---|
| `tests-js/` | Vitest + jsdom | Each handler (login submit, sender submit, reveal) run against a DOM fixture; in-flight guards, error-path button restore, success-path state swap |
| `tests-e2e/` | Playwright | Golden path (login -> create text secret -> open URL in a second browser context -> reveal -> second visit shows "gone") |

Seed and boot live in `tests-e2e/start.sh` (wipes a scoped DB, provisions a
known user with a fixed TOTP secret, then execs uvicorn on a test port).
