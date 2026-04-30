# AGENTS.md

Operating rules for any agent (human or AI) contributing to this repo.

Ephemera is a self-hosted, single-admin one-time-secret service: the
sender encrypts a payload, the recipient gets a single-use URL, the
secret is destroyed on first read. Stack: FastAPI on SQLite (no ORM),
plain HTML/CSS and unbundled JS on the front end (no SPA framework,
no client build step). Source layout: `app/` is the FastAPI service
(routes, models, templates, static assets); `tests/` is pytest;
`tests-js/` is Vitest for the front-end JS; `scripts/` holds operator
and release utilities.

This file is the contract: read it before opening a branch, follow it
while working, and re-read it before pushing. It is intentionally short.
The rules are framework- and tool-agnostic; the tooling that *currently*
implements them lives in `README.md`, `package.json`, `pyproject.toml`,
and the CI workflows under `.github/workflows/`. Product context — what
the thing does, why it works the way it does, the operational and
deployment recipes — lives under `docs/`; read what's relevant to the
area you're touching before you change it, and update those same docs
in the PR that changes the behaviour, so the documentation stays in
step with the code instead of drifting a release behind. When the
stack changes, the rules survive — the commands underneath them get
updated.

Nothing here is sacred: every rule is open to discussion. Disagreement
is welcome, silent bypass is not. If a rule gets in the way of a real
problem, raise it, decide together, then update this file in the same
PR that changes the behaviour.

---

## 1. Test-first (TDD)

Write the test before the production code. Not "alongside", not "after the
fix is in" — first.

The order is:

1. **Reproduce.** Write a failing test that exercises the behaviour you
   intend to add or fix. Run it. Confirm it fails for the *expected*
   reason (assertion message, not import error).
2. **Implement.** Make the smallest change that flips the test green.
3. **Cover the neighbours.** Add tests for the obvious adjacent cases
   (empty input, boundary, error path, the one a malicious caller would
   try). Bug fixes get a regression test that would have caught the bug.
4. **Refactor.** Only once the suite is green.

Why test-first, not test-eventually:

- A test written after the code tends to assert what the code happens to
  do, not what the feature is supposed to do. The bug it would have
  caught is now baked in as "expected".
- "I'll add the test next" reliably becomes "I'll add the test next PR"
  and then "the coverage was already passing".
- Reproducing a bug *as a test* is the cheapest way to confirm you
  understand it. If you can't write the failing test, you don't yet know
  what's broken.

Carve-outs (small, named, explicit):

- **Pure exploration / spike.** You're learning the shape of an API or a
  library. Fine — but the spike doesn't merge. The PR that lands the
  feature starts from a failing test.
- **Untestable surface.** Some changes (visual polish, a one-line copy
  tweak, a config knob) genuinely don't have a meaningful unit-level
  assertion. Say so in the PR description and route it through a human
  reviewer instead. Don't invent a tautological test to satisfy the
  rule.
- **Truly trivial typo / comment fix.** No test needed; also no review
  surface, so keep these PRs surgical.

Anything outside those carve-outs ships with a test that was red before
the implementation existed.

---

## 2. Wire the test infrastructure first

When you add a new *kind* of thing — a new layer, a new entry point, a
new external integration — the first commit on the branch wires up how
that thing will be tested. Examples: a new background worker gets a test
harness before it gets logic; a new HTTP route gets a request-level test
file before the handler body fills in; a new CLI command gets an
invocation test before the argparse wiring.

If you find yourself writing production code in a layer that has no
test scaffolding, stop and add the scaffolding first. "I'll add a test
file later" is how layers end up untested forever.

---

## 3. Acceptance tests are the spec

`tests-e2e/` (the Playwright suite) is the system's acceptance layer:
a black-box description of what ephemera *does* end-to-end, written
from the user's perspective. The rest of the test suite — pytest
under `tests/`, Vitest under `tests-js/` — describes how the
implementation is built; the acceptance suite describes what the
product is.

This distinction matters more in AI-assisted development than it does
otherwise. Implementation-aligned tests can be co-written with the
code they cover, and the same agent that produces the change tends to
produce tests that reflect the change rather than the original intent.
The acceptance suite is the only test layer that sits *outside* the
code-and-test pair an agent might modify together — it's the line in
the sand against silently moving the goalposts.

The rule:

- **The default response to a bug is to change the implementation
  until the existing acceptance test passes**, not to change the
  acceptance test until the new implementation passes.
- **If a feature genuinely requires the acceptance test to change
  shape**, that change ships as a *separate first commit*, reviewed
  by a human on its own merits, before the implementation change
  lands. The PR description names the acceptance change explicitly so
  it isn't read as part of the implementation diff.
- **`tests-e2e/` is not modified to make a failing test pass when the
  implementation breaks**. A red e2e test is a signal the
  implementation regressed; the implementation is what gives.

The same principle applies, in a softer form, to other tests that
function as specs rather than implementation-mirroring assertions
(security invariants in `tests/test_security.py`, audit-trail
invariants in `tests/test_security_log.py`, the privacy invariants
documented in `tests/test_analytics.py`). When in doubt about whether
a test is a spec or an implementation aid, treat it as a spec — the
cost of asking is cheaper than the cost of silently weakening the
contract.

---

## 4. Before you commit

Every commit must pass, locally, the same gates CI will run on it.
Discovering a lint failure on a CI round-trip is a 4–5 minute waste of
everyone's time when the local run is sub-second.

Run, in order:

1. **Linter / formatter** for every language touched in the diff.
2. **Unit tests** for the package(s) you changed. Full suite if the
   change is cross-cutting.
3. **Type checker / static analysis**, if the language has one
   configured.
4. **End-to-end / integration tests** when the change touches a path
   they cover (auth, request handling, deploy plumbing, schema, anything
   user-visible).

If you bumped or added a dependency, also run a clean install from the
lockfile (not the incremental update) — incremental installs routinely
hide missing entries that break CI on a fresh checkout.

**Stage files explicitly, by name. Never `git add .`, `git add -A`, or
`git add -u`.** Wholesale staging is how `.env` files, editor
swapfiles, scratch notes, screenshot dumps, debug logs, and accidental
edits in unrelated files end up committed. The cost of typing the paths
is trivial; the cost of unstaging a leaked secret from history is not.
If a commit really does need every changed file, list them — the act of
listing is the audit. The same applies to `git commit -a`: don't.

A failing pre-commit hook means the commit didn't happen. Fix the
underlying issue and create a *new* commit. Don't `--amend` past a
failed hook (the previous commit is what gets amended, not the one you
thought you were making) and don't pass `--no-verify` to skip the gate.
If a hook is genuinely wrong, fix the hook in its own commit.

---

## 5. Security is part of "done"

A feature is not finished when it works on the happy path. It's finished
when you've considered, and have an answer for, the obvious failure
modes:

- **Authentication & authorisation.** Who can call this? What if they
  lie about who they are? What if a logged-in user pokes at someone
  else's resource id?
- **Input you don't control.** Anything from a request body, query
  string, header, cookie, uploaded file, or environment variable is
  hostile. Validate at the boundary, then trust internal callers.
- **Output that crosses a trust boundary.** Logs, error messages, HTML,
  redirects, external API calls. Don't leak internal state, stack
  traces, user records, or secrets through any of them.
- **Concurrency and replay.** What happens if this runs twice? If two
  callers race? If the same token is presented twice?
- **Failure modes.** What does a downstream timeout look like? A full
  disk? A truncated upload? Failing closed (deny) is almost always safer
  than failing open (allow).

For anything non-trivial that could plausibly affect the security
posture — auth, sessions, crypto, rate-limiting, error surfaces, file
upload, a new external dependency, schema changes that touch sensitive
columns — get a second pair of eyes *before* you commit, not at PR
review time. Late security feedback turns into rework; early feedback
turns into design.

User-facing error copy on auth failures should not distinguish *why* a
credential was rejected. "Invalid credentials" is the canonical surface;
per-factor wording (wrong password vs. wrong TOTP vs. unknown user)
gives an attacker a free oracle.

---

## 6. Personal information deserves a conversation

Any change that captures, stores, links, or emits information about
real users — identifiers, IPs, content payloads, telemetry, audit
logs — deserves an explicit moment of thought before it lands. Default
to minimization: don't collect what you don't need, don't link what
doesn't have to be linked, don't keep what doesn't have to be kept.

When a change adds or expands handling of personal data, raise it
explicitly with the user before designing around it. Typical triggers:

- A new event, metric, or log line that carries (or could be joined
  back to) a user identifier.
- A schema column that ties a row to a person, or a widening of one
  that already does.
- A field surfaced in an error, response, or external call that didn't
  previously cross that boundary.
- Default-on collection or retention.

These are conversations, not vetoes. If a feature genuinely needs a
particular shape, that's the right answer — but it's a decision made
together, not an inferred shortcut. Past decisions in this area
(e.g. the aggregate-only, opt-in posture in `app/analytics.py`) are
decisions, not law: if a future feature has a real reason to revisit
them, the discussion comes before the diff, not after.

---

## 7. Triple-check for leaks before anything goes public

This is the rule with zero tolerance. Anything that goes into a public
artifact — a commit, a PR title or body, a comment, a release note, a
screenshot, a log uploaded as a CI artifact, a doc, an issue — is
treated as published the moment it exists. You cannot delete a leaked
secret out of git history without a coordinated rewrite, and you cannot
delete it from someone's mirror at all.

**Three passes, in this order, every time:**

1. **Scan the diff.** Look at every changed line, including the parts
   you didn't intend to touch. Tokens, API keys, passwords, session
   cookies, private URLs, hostnames or IPs of internal/staging/prod
   infrastructure, real user data (emails, usernames, names),
   `.env`-style files, anything from `~/`, anything that came out of a
   secret manager. The explicit-staging rule from §4 exists exactly to
   make this scan possible — review the actual list of files in the
   commit, not "whatever was dirty in the tree".
2. **Scan the metadata.** PR title, PR body, commit message, branch
   name, screenshot filenames, attached images. A screenshot of a
   working feature very often includes a real session, a real token in
   a URL, a production hostname in the address bar, or another user's
   name in a sidebar. Crop or redact before attaching. Internal
   tracking codes (audit IDs, ticket numbers from private trackers)
   stay in private notes — never in public titles, bodies, or source
   comments.
3. **Scan the surroundings.** Test fixtures, log captures, VCR
   cassettes, `.har` files, debug dumps, sample data, error pages saved
   for reproduction, anything under a `tmp/` or `scratch/` path that
   might have been added by accident. CI artifacts uploaded on failure
   are public on a public repo.

If you find a leak *after* pushing, treat it as an incident: rotate the
credential first (the one that was exposed is gone, period), then deal
with the history. Don't try to quietly force-push it away — the
credential has already been scraped.

When in doubt, keep it out. A second commit to add something back is
free; a leaked secret is not.

---

## 8. Commits, branches, and PRs

**Commit messages** are imperative one-sentence summaries that explain
the *why*, not just the *what* — "Pin pip in the runtime lockfile so
deploy.sh keeps the VPS pip current" rather than "Update
requirements.txt". Capitalise the first letter, no trailing period,
no Conventional-Commits prefix (`feat:`, `fix:`, `chore:`), no ticket
or audit IDs in the subject. If a single sentence isn't enough, add a
body separated by a blank line.

**Branch names** are short kebab-case summaries of the intent
(`tokenization-sweep`, `drop-pip-cve-ignore`,
`char-limit-ux-and-telemetry`). When a branch is being driven by a
specific contributor, prefix with their GitHub handle
(`JoaoPucci/drop-pip-cve-ignore`). Don't reuse a branch name across
unrelated work — once a branch has shipped, the next change starts a
new one.

**Every PR opens with at least one meaningful label and a
self-assignee.** Labels exist so a future contributor scanning the
queue can tell what's in flight without opening each one — pick what
already fits, don't stretch a label past its meaning. If the right
category genuinely doesn't exist yet, propose a new label (or surface
the gap in the PR) rather than shoehorn one of the existing ones. New
labels use the official GitHub-recommended palette so the colour
scheme stays coherent and labels stay visually scannable; pick the
closest semantic match from that palette rather than inventing a hex.

---

## 9. Evolving these rules

These rules exist because past mistakes made them necessary. If one of
them is friction without payoff in a specific case, that's information
— surface it. The path is:

- Raise the concern in the PR (or an issue, if it's broader).
- Decide together what the new rule should be, or why the existing one
  still applies in this case.
- If the rule changes, update *this file* in the same PR that changes
  the behaviour. Out-of-date rules are worse than strict ones —
  contributors lose trust in the document, and then in the practices it
  was meant to encode.

Past decisions recorded here are decisions, not constraints. They can
be revisited; they cannot be silently ignored.
