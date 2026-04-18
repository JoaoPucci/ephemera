# Proposal: admin role and panel

**Status**: draft — seeking feedback
**Current system**: see [`ARCHITECTURE.md`](../../ARCHITECTURE.md)
**Discussion**: open a GitHub issue or discussion thread. This is a
direction, not a commitment.

---

## TL;DR

Today, "admin" in ephemera is implicit: whoever has shell access holds the
keys. There's no role in the database, no admin page, no way to manage users
without SSH. This proposal adds an explicit admin role, moves user
management to an `/admin` page, and documents the new threat model that
comes with it — because once destructive actions can fire from a web
session, the value of hijacking that session goes up and the compensating
controls have to match.

The change is less about features and more about trading one security
property (a stolen web session can't hurt anyone else) for another (you can
administer the service without being on SSH). That's a real trade, and this
proposal is where it gets discussed rather than quietly made.

---

## Why

The existing "shell = admin" model has been intentional since day one
(decisions #11, #16 in the architecture doc). Its virtues:

- **A hijacked web session is strictly limited to that user's own data.**
  There's no "admin mode" for an attacker to slip into.
- **No "first signup becomes god" race** (the Gitea pattern).
- **The UI stays quiet.** The design philosophy in
  [`docs/requirements.md`](../requirements.md) pushes against dashboard
  energy.

What it costs you in practice:

1. **Destructive ops on other users require SSH.** Every "remove this user,"
   "reset their password," "revoke their tokens" action means a terminal.
   The [`remove-user --force`](../../README.md#admin-cli-reference) escape hatch
   shipped as a small mitigation, but it's still CLI-only.
2. **No visibility into the instance.** You can't glance at "who's using this,
   how many secrets each person has, when did each person last log in" —
   those are SQL queries you write by hand.
3. **Administering from a phone is painful.** SSH-from-mobile is doable, but
   not casual. Running a small instance for friends without a laptop
   handy means you can't help them.
4. **No audit trail.** When the `remove-user --force` command runs, it
   leaves no record other than the absence of the target in `list-users`.

For "me + a few friends" on one laptop, none of this matters much. For
"a handful of friends who occasionally need their accounts managed while
I'm on the train," it starts to.

---

## What users would notice

### Regular users (non-admins)
Nothing visible. The `/send` page looks identical. No new menus, no new
prompts, no new "you don't have permission to X" errors. Their sessions
behave exactly as today.

### Admin users
A new top-right pill labeled `admin` next to the existing user pill.
Clicking it opens `/admin`, a separate page in the same visual language as
the rest of the app (same card, same palette, same quiet tone).

The `/admin` page contains three tabs:

**Users** — table of every account with:
- Username, email, is_admin flag
- Created date, last login
- Counts: # pending secrets, # viewed secrets, # API tokens
- Per-row actions: reset password, rotate TOTP, regen recovery codes,
  promote/demote admin, delete
- Each destructive action opens a modal that requires the admin to
  re-enter their own password + TOTP — a valid session is not enough.

**Tokens** — a flat list of every API token across all users, with
last-used timestamps. Primarily a "is any of my friends' tokens stale?"
lens. Revocation from the panel.

**Audit log** — append-only record of admin actions: who did what, to whom,
when, from what IP. Each row also shows the admin's browser fingerprint
(sha256 of user-agent + accept-language). Append-only means the admin
can't delete their own entries via the panel. (Deleting them at the SQL
level is still possible for anyone with shell — that's the escape hatch
and it's accepted.)

---

## Architecture

### Schema additions

```sql
ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0;
-- First user provisioned by the CLI gets is_admin=1 at creation time.
-- Existing DBs on migration: whoever has id=1 gets promoted.

CREATE TABLE admin_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER NOT NULL REFERENCES users(id),  -- NOT cascade: delete admin != delete their audit
    action        TEXT NOT NULL,          -- 'delete_user', 'reset_password', 'rotate_totp', 'regen_recovery', 'promote', 'demote', 'revoke_token'
    target_user_id INTEGER,               -- NULL for non-user-targeting actions
    target_username TEXT,                 -- denormalized: survives a target delete
    metadata      TEXT,                   -- JSON: anything action-specific
    ip_address    TEXT NOT NULL,
    user_agent_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_admin_audit_created_at ON admin_audit(created_at);
CREATE INDEX idx_admin_audit_admin ON admin_audit(admin_user_id);
```

### Routes (new)

```
GET  /admin                            -- the page itself; 404 for non-admins (indistinguishable from "no such route")
GET  /api/admin/users                  -- list all users with counts
GET  /api/admin/tokens                 -- flat token list across users
GET  /api/admin/audit                  -- paginated audit log
POST /api/admin/users/{id}/delete      -- requires sudo token (see below)
POST /api/admin/users/{id}/reset-password
POST /api/admin/users/{id}/rotate-totp
POST /api/admin/users/{id}/regen-recovery-codes
POST /api/admin/users/{id}/promote
POST /api/admin/users/{id}/demote
POST /api/admin/tokens/{id}/revoke
POST /api/admin/sudo                   -- fresh password + TOTP -> short-lived elevated token
```

### Sudo-style elevation

A plain admin session does **not** authorize destructive actions. Instead:

1. Admin clicks a destructive action (e.g., "delete user").
2. UI opens a modal asking for password + TOTP.
3. Client POSTs to `/api/admin/sudo` with those credentials.
4. Server verifies (same `authenticate()` flow as login) and returns a
   short-lived (~5 min) `sudo` token — stored in sessionStorage or a
   separate cookie.
5. Client sends the sudo token with the destructive action's POST.
6. Server validates the sudo token, runs the action, writes an audit row.

This means: hijacking an admin session buys the attacker only the ability
to view things (list users, view audit). To actually change state they
need the admin's password + TOTP in real time, which is the same bar as
the CLI today. A XSS'd or stolen session cookie is meaningfully less
dangerous than it would be without this.

### First-user-is-admin (with a safety net)

- The CLI's `init <username>` sets `is_admin=1` on the first user, same
  as today except explicit.
- `add-user <username>` creates regular users (is_admin=0).
- Migration: whoever has `id=1` in an existing DB gets `is_admin=1`.
  Everyone else starts as regular; existing admins would promote their
  trusted friends from the panel once shipped.
- **No public signup path ever promotes to admin.** This keeps decision
  #16's "first signup becomes god" resistance intact.
- `promote`/`demote` require sudo, as above.
- The last remaining admin cannot demote themselves or delete themselves —
  same spirit as "refuse to remove the only user."

---

## Design decisions

- **Sudo tokens, not session-wide elevation.** A single elevated-session
  cookie from login time would mean a stolen cookie is game over. Sudo
  tokens make every destructive action re-prove credentials.
- **Audit log is append-only from the panel, not from SQLite.** Protection
  against a careless admin, not against a determined one with shell. That
  level of defense is another conversation.
- **No two-admin requirement for destructive actions.** Considered
  (require two admins to sign off on a delete, like root-of-trust
  ceremonies). Rejected for this scale: too much friction for a
  friends-and-family instance.
- **404, not 403, for non-admins hitting `/admin`.** Mild
  fingerprint-resistance: you can't tell from outside whether the
  instance even has admin capability turned on.
- **No self-service signup comes with this proposal.** That's a separate,
  even bigger decision (decision #15's "step C"). Admin-panel without
  signup means the panel is purely for managing CLI-provisioned users.

---

## Threat model

### What this proposal changes

| | Today | With admin panel |
|---|---|---|
| Hijacked regular-user session | Can create/read that user's own secrets | Unchanged |
| Hijacked admin session | (Doesn't exist — admin is shell) | Read-only admin page + audit. NO destructive action without sudo. |
| Stolen admin password alone | Useless without TOTP | Useless without TOTP (sudo requires both) |
| Stolen admin password + TOTP | Would need also the laptop (SSH) | Can log in via web, obtain sudo, do anything |
| Shell access to the droplet | Full control | Full control (unchanged) |
| SQL access to the DB | Full control | Full control (unchanged) |

The most important row is the second: a hijacked admin session in the new
model is *read-only* until the attacker can intercept a fresh password +
TOTP exchange. That's a narrower attack surface than "admin session = total
control," which is the naive implementation most projects ship.

### Residual risks that still exist

- **Admin's TOTP is now phishable over the web.** Today it's only used
  at SSH prompts, which most phishing flows don't target. With the panel,
  TOTP codes get typed into browser forms routinely, so a fake
  `ephemera.example.com` page could capture one. Mitigation: the same
  `Origin`-header CSRF defense already in place stops cross-origin
  submissions; users who reuse their TOTP on a phishing domain are beyond
  the protocol's reach.
- **Sudo-token theft via XSS.** If an attacker can run JS in the admin's
  browser, they can steal a live sudo token and make a destructive call
  in the ~5-minute window. Mitigation: CSP already forbids inline
  scripts; if XSS is happening, there are bigger problems.
- **Audit evasion via SQL.** Anyone with shell can rewrite or delete audit
  rows. Accepted — "shell = god" is the deliberate choice.

### What this proposal does NOT address

- **No email-based password reset.** Admin can reset another user's
  password via the panel, but there's no self-service "I forgot my password"
  flow reachable without an admin's help.
- **No rate-limit bypasses for admins.** Admins are subject to the same
  rate limits as everyone else. Good — prevents admin-session stuffing
  from chewing through the lockout budget undetected.

---

## Migration plan

Phased. Each phase leaves the tree green and passing tests, but only
after phase 4 is the feature user-visible.

### Phase 0 — design decisions

- Confirm the sudo-token lifetime (5 min is a guess; could be 10, could
  be 60 seconds).
- Decide whether regular users should see *any* trace of admin
  existence (recommendation: no).
- Decide whether the audit log should be available to regular users
  for their *own* account (recommendation: yes, "view my history" is
  fair — but out of scope for this proposal's first cut).

### Phase 1 — schema + migration

- Add `users.is_admin` with backfill for `id=1`.
- Create `admin_audit` table.
- Update `init` to mark `is_admin=1`; `add-user` stays as-is (regular).
- New model-layer functions: `list_users_admin_view`, `promote_user`,
  `demote_user`, `append_audit`, `paginate_audit`.
- Tests: migration on a legacy DB, role isolation (regular user can't
  set is_admin via SQL-injection-like paths through the app's own API).

### Phase 2 — sudo tokens + admin routes (read-only)

- `POST /api/admin/sudo` — mints a short-lived token if fresh password +
  TOTP check passes.
- `GET /api/admin/users`, `GET /api/admin/tokens`, `GET /api/admin/audit`
  — read-only, require admin session.
- Write-side routes (delete, reset, rotate, ...) stub out with
  `require_sudo` dependency that 403s until phase 3.
- Tests: sudo-token expiry, admin vs non-admin access, audit writes
  happen on every successful sudo exchange.

### Phase 3 — destructive actions behind sudo

- Each destructive action:
  - validates the sudo token,
  - performs the change,
  - writes an audit entry with `metadata` describing what changed.
- Tests for each action's idempotence, cascade (for delete), and audit
  trail.

### Phase 4 — the `/admin` page

- Three-tab layout (Users / Tokens / Audit).
- Sudo modal component reused by every destructive action.
- Top-right `admin` pill on `/send` for admin users, hidden for regular
  users.
- Tests: Vitest for the modal + table logic, one Playwright smoke test
  that logs in as admin, opens `/admin`, and runs a round-trip
  "promote then demote" cycle to prove the whole stack hangs together.

### Phase 5 — docs + release

- Update `ARCHITECTURE.md` decisions log (new entry: "admin role in the
  app, sudo-gated, audited").
- Update `docs/backend.md` with the new routes, schema, and sudo flow.
- Update `docs/frontend.md` with the admin page layout.
- Update `docs/deployment.md` if operational surface changes (probably
  does not).
- Tag a minor release.

### Effort estimate

Rough: a week of focused evenings. Larger than the E2E proposal because
it involves new UI, new routes, a new token flow, and careful
permission-boundary testing. Not a weekend project.

---

## Trade-offs

### Security trades

- **Lose:** "A hijacked session is strictly this user's own scope."
  Admin sessions now carry more authority, even with sudo gating.
- **Gain:** Audit trail. Today's "shell + SQL" model leaves no
  application-level record of who-did-what. Some of that tooling around
  observability (including the audit log) is valuable independently of
  whether you ever use the panel to actually delete anyone.

### UX trades

- **Lose:** The quiet-by-design philosophy ships with a "dashboard."
  Mitigation: apply the same design language, keep it on a separate
  route, never surface it to regular users. It remains "quiet" within
  its own page.
- **Gain:** Administering from a phone becomes realistic. The SSH-first
  workflow has been quietly coloring what features feel "ok" vs "too
  much" — lifting that constraint changes the design space downstream.

### Code trades

- **Lose:** Meaningful growth in surface area. Estimate: ~600 lines
  Python (routes + models + audit), ~400 lines JS (admin page +
  sudo modal), ~100 lines test code per functional area. Schema adds
  one column + one table.
- **Gain:** A foundation for the "decision #15 step C" world (public
  signup), should that ever become a goal. Most of the plumbing needed
  for signup — admin oversight, audit trail, per-user management — is
  already here.

### Forever trades

- **Commitment:** Once admin ops live on the web, backing them out is
  disruptive. This proposal is less reversible than adding a UI polish.

---

## Open questions

Feedback especially welcome on these:

1. **Sudo-token lifetime.** 5 minutes is conservative. 15 is more
   ergonomic if an admin is doing a batch of changes. 60s is
   paranoid-but-not-unreasonable. Which feels right for this audience?
2. **Should regular users see their own audit log?** (Who reset my
   password, when was my TOTP last rotated.) Nice feature, but
   infrastructure to expose it doubles.
3. **Should admin demote-self be allowed if another admin exists?**
   Today's CLI refuses to empty the users table; admin-demotion has
   a similar "don't lock yourself out" instinct.
4. **Sudo-token carrier: cookie vs sessionStorage vs bearer header?**
   Each has trade-offs around XSS, CSRF, and same-site restrictions.
   The simplest is a second cookie with short `Max-Age`; debate welcome.
5. **Should there be a read-only "inspector" role** in addition to
   admin? (Someone who can view counts and audit but not mutate.) Adds
   complexity; may not be worth it at friends-and-family scale.

---

## Prior art

- **Django admin** — classic, general-purpose, very flexible, much more
  than ephemera needs. Good reference for "what does an audit model
  look like."
- **Vaultwarden** (unofficial Bitwarden server) — has a separate
  admin panel at `/admin` gated by a token that's set in a config file.
  Token-based admin is simpler but clumsier than role-in-DB.
- **Gitea** — the example decision #16 pushed against. First signup
  becomes global admin; users have had to disable signup on public
  instances to avoid takeovers. Good negative example.
- **Mastodon / Pleroma** — full role hierarchies (user / moderator /
  admin / owner). More than needed here, but the audit log patterns
  are mature.

ephemera's version would be closer to Vaultwarden in shape (single admin
role, small surface) but role-in-DB in mechanism (avoids the "who holds
the magic token" problem).

---

## How to give feedback

Open a GitHub issue with one of these labels:

- `proposal-feedback` — general thoughts.
- `threat-model` — if you think the security reasoning has a hole.
- `ux-concern` — if the panel layout or sudo flow reads wrong to you.
- `scope` — if you think this should be smaller or bigger than drafted.

Feedback is most valuable *before* Phase 0 decisions are locked in.
After implementation starts, the conversation shifts from "should we?"
to "should we revert?"

---

## Changelog

- **2026-04-18** — initial draft.
