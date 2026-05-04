# Security policy

## Reporting a vulnerability

Use **GitHub Security Advisories** for the canonical channel:

- Repository → **Security** tab → **Report a vulnerability**.

That gives you a private, audit-trailed thread with the maintainer
and lets us coordinate a fix-and-disclose without anything
becoming public until we agree it should.

If you cannot use GHSA (no GitHub account, account suspended,
etc.), email **dev@thirdmovement.net** with the subject prefix
`[ephemera-security]` so it routes cleanly.

When you report, please:

- **Use synthetic credentials in your proof-of-concept.** Real
  session cookies, `eph_`-prefixed API tokens, recovery codes,
  TOTP secrets, or live secret-URLs all get ingested by the
  application — replaying them lands genuine secrets in our audit
  log (`app/security_log.py`) or at-rest storage. Mint disposable
  values via the admin CLI or stub them in the report.
- **Name the deployed version**, ideally a tag or a commit SHA.
- **Include the minimal reproduction**, not the toolchain that
  produced it.

Expect an initial acknowledgement within **72 hours**. We do not
commit to a fix-by date — ephemera is solo-maintained, and a
realistic timeline depends on the severity, the reproducibility,
and how much of the attack surface the fix touches. We will keep
you updated on the GHSA thread (or by email) as the fix moves.

## Supported versions

Only the **latest tagged release** on `main` is supported.

There is no LTS line and no backporting; if a fix is required on
an earlier tag, the answer is to upgrade. The auto-deploy pipeline
(`.github/workflows/deploy.yml`) verifies the test-suite + audit
surface against the tagged ref before touching the droplet, so
running the latest tag is the supported path.

## Scope

**In scope:**

- The FastAPI application under `app/`.
- The admin CLI (`app/admin/`).
- The auto-deploy workflow and the in-repo deploy script
  (`.github/workflows/deploy.yml`, `scripts/deploy/`).
- Authentication, rate-limiting, audit logging, telemetry
  (aggregate-only, opt-in), and the cryptographic surface in
  `app/crypto.py` / `app/auth/`.

**Out of scope:**

- The `audit/` directory — private, gitignored by design; not
  shipped, not built, no surface to attack.
- Operator misconfiguration. The e2e test-hooks router
  (`app/_test_hooks.py`) is gated behind `EPHEMERA_E2E_TEST_HOOKS`
  and is a deliberate test-only surface; an operator who turns it
  on in production has chosen to expose those endpoints. Same for
  `EPHEMERA_TEST_BCRYPT_ROUNDS_OVERRIDE` and other `EPHEMERA_*`
  env vars. Reports against deployments that have flipped these
  intentionally are operator-policy issues, not product
  vulnerabilities.
- Reports against tags that are no longer the latest release.
- Upstream library vulnerabilities. These should be reported to
  the library directly; our handling of a fixed upstream version
  is in scope once a CVE is published and a pinned bump is
  available.

## Lived invariants

Before filing, it can be worth checking the project's
spec-shaped tests — the answer to "is this behaviour intended"
is often there:

- [`docs/threat_model.md`](docs/threat_model.md) — what's in
  scope to defend, what's deliberately not, and the known
  posture observations (token-in-access-log, receiver
  anonymity, audit-log retention).
- `tests/test_security.py` — security headers, CSP, CSRF gate,
  auth response-shape invariants (canonical "invalid credentials"
  on every auth-failure path), rate-limiter behaviour.
- `tests/test_fitness_functions.py` — AST-level architecture
  invariants: every state-mutating route carries an origin gate
  AND a rate limiter (in that order), `totp_secret` reads only
  inside `with_totp`-named getters, source-pinned `BCRYPT_ROUNDS`,
  no `print()` in the request path, etc.
- `AGENTS.md` — the operating contract, including `§5. Security
  is part of "done"` (the failure-mode checklist applied to every
  feature) and the generic-credentials and rate-limit invariants
  the codebase pins.

If the behaviour you're reporting matches one of those documented
invariants, the report is welcome — we'd rather a closed-as-by-design
ticket than a silent bypass.
