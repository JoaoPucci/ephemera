# Requirements

What ephemera does, who it's for, and the product-level decisions that shape
every other piece of the design. Technical deep-dives live in
[`backend.md`](backend.md), [`frontend.md`](frontend.md), and
[`deployment.md`](deployment.md); the index in
[`../ARCHITECTURE.md`](../ARCHITECTURE.md) has the decisions log and links
everything together.

## What it is

A self-hosted one-time secret (OTS) sharing system. A user ("sender") creates a
secret — text or image — and gets a URL. The URL is sent to a recipient
("receiver"), out of band. The recipient opens the URL, clicks **Reveal**, and
sees the secret exactly once. After that, the secret is destroyed.

## Design goals

- **Ephemeral by default.** Everything has a TTL. Even the metadata of a
  "tracked" secret gets purged after 30 days.
- **Zero-knowledge at rest.** A database dump alone cannot reveal a secret's
  content. (This is enforced by splitting the encryption key between the DB and
  the URL fragment — see the crypto design in [`backend.md`](backend.md).)
- **Self-hosted and minimal.** Single SQLite file, single Uvicorn worker, no
  Docker, no Redis, no queue, no ceremony.
- **Quiet UI.** The app should feel like unsealing an envelope, not logging
  into a dashboard. No gradients, no SaaS energy, no footer, no "powered by."

## Features

| Feature | Summary |
|---|---|
| Text secrets | Plain text, multi-line, preserved whitespace on reveal. |
| Image secrets | PNG / JPEG / GIF / WebP, up to 10 MB. SVG explicitly rejected (XSS vector). |
| One-time viewing | Secret is destroyed on successful reveal. Second visit shows "gone." |
| Passphrase protection | Optional; wrong passphrase burns after 5 attempts. |
| Auto-expiry | Configurable from 5 minutes to 7 days. Default 24 hours. |
| Tracking | Sender can opt-in to see `pending / viewed / burned / canceled / expired` status per secret. |
| Sender-initiated cancel | Revoke a still-live secret from the sender's tracked list. |
| Labels | Sender-supplied nickname for a tracked secret, to identify it later. |
| Multi-user | Users and their secrets are isolated. Users are provisioned via CLI by an existing user. |
| Auth | Password + TOTP with ±1 step tolerance, plus 10 one-time recovery codes. |
| API tokens | DB-issued, revocable bearer tokens for programmatic use. |
| Light/dark themes | CSS custom properties + `[data-theme]`; persisted per-browser. |
| Click-to-zoom | Revealed images open full-screen on click. |

## Roles

- **Sender**: authenticated user of the web form at `/send`. Creates secrets,
  tracks them, cancels them.
- **Receiver**: anyone with the URL. Unauthenticated — the URL itself is the
  authorization. The receiver only ever sees a landing page, optionally a
  passphrase prompt, and the one-shot content.

## User flow (high level)

The [technical sequence diagrams](backend.md#core-flow) live in `backend.md`.
This is the prose version:

1. Sender signs in at `/send` with username, password, and a TOTP code.
2. Sender types a message (or drops an image), optionally sets a passphrase,
   optionally sets a tracking label, picks an expiry, and submits.
3. Server returns a URL of the form `https://host/s/{token}#{key_fragment}`.
   The fragment is never sent to the server in normal browsing — that's how
   key splitting works.
4. Sender copies the URL and delivers it to the recipient out-of-band (email,
   chat, paper). The passphrase, if set, is delivered on a separate channel.
5. Recipient opens the URL. Landing page explains "someone shared a secret
   with you," shows a **Reveal Secret** button, and a passphrase input if the
   secret has one.
6. Recipient clicks Reveal. The content appears, rendered in place. At the
   same instant, the server destroys the secret.
7. Recipient either copies the content (for text — a clipboard button is
   provided) or views/downloads the image.
8. Reloading the URL now shows "this secret is no longer available."
9. Sender, if they enabled tracking, sees the status change in their own
   list — confirmation the secret was actually viewed.

Failure modes that the product explicitly handles:

- Wrong passphrase → increments a counter; five wrong tries and the secret
  self-destructs with a clear error.
- Expiry reached → secret is purged by a background task; URL returns 404.
- Sender-initiated cancel → URL stops working immediately; tracked row
  survives as `status='canceled'` if tracking was on.
- Concurrent reveal attempts → atomic at the DB level; only one succeeds,
  others get 404.

## Expiry presets

Chosen at creation time from a fixed dropdown:

| Label       | Duration |
|-------------|----------|
| 5 minutes   | 300s     |
| 30 minutes  | 1800s    |
| 1 hour      | 3600s    |
| 4 hours     | 14400s   |
| 12 hours    | 43200s   |
| 24 hours    | 86400s   |
| 3 days      | 259200s  |
| 7 days      | 604800s  |

Default: 24 hours. No custom-duration option — preset-only keeps the UX
simple and the test surface small.

## Design philosophy

The name "ephemera" refers to things that are transient and fleeting — old
tickets, handwritten notes, letters meant to be read once. The UI leans into
this: quiet, paper-like, unhurried. No flashy gradients or corporate SaaS
energy. It should feel like unsealing an envelope, not logging into a
dashboard.

Concrete consequences of that philosophy, enforced in the frontend design
(see [`frontend.md`](frontend.md)):

- A single centered card on every page — no chrome, no navigation bars.
- The wordmark "ephemera" is small and muted, not a loud logo.
- The Reveal button is the only loud element on the receiver page. Every
  other visual takes a step back so attention goes there.
- Technical jargon (encryption, keys, fragments) never appears in the
  receiver-facing UI. Those details are the *how*, not the *what*.

## What ephemera does NOT do

Deliberate non-features. Calling these out so the scope doesn't drift:

- **No signup page.** Users are provisioned via the admin CLI. Public signup
  is out of scope (and explicitly so — see decision #16 in
  [`../ARCHITECTURE.md`](../ARCHITECTURE.md)).
- **No content preview before reveal.** The receiver doesn't learn anything
  about the secret (type, length, hint) before clicking reveal.
- **No chat or reply.** One-shot delivery; there's nothing for the receiver to
  send back.
- **No file uploads beyond images.** No PDFs, no zips, no arbitrary blobs.
  Text and images cover the 95% case and keep the MIME validation story
  small.
- **No animations beyond the reveal fade-in, status pulse, and button hover
  transitions.**
- **No JavaScript frameworks.** Vanilla JS only, under a few hundred lines
  total. No build step.
- **No external resources** — fonts, CDNs, analytics, icons. Fully
  self-contained. Privacy-preserving by construction.
- **No footer, no "powered by", no version number** in the UI. The page is
  just the card.
- **No password-reset flow.** If you lose your password AND your TOTP AND
  your recovery codes, the answer is "wipe and re-provision that user." No
  email-based reset, no security questions.
- **No forgotten-passphrase recovery.** If the sender set a passphrase and
  the receiver forgets it, the secret burns on the fifth wrong attempt. By
  design.
