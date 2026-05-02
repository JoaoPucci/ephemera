// Stryker mutation testing config.
//
// Read by .github/workflows/mutation-test-js.yml's weekly run and by
// operators triggering manual runs via `npx stryker run`. Stryker
// applies a set of mutation operators to each file in `mutate`,
// re-runs the Vitest suite per mutated source, and records each
// mutant as killed (some test failed) / survived (every test still
// passed) / timed-out / no-coverage.
//
// Mirror of cosmic-ray.toml on the Python side. Differences vs. the
// Python harness:
//
//   - Vitest's per-test runtime is microseconds (no bcrypt, no
//     SQLite I/O), so we don't need the 1800s per-mutant timeout
//     cosmic-ray needs. Stryker's default 10s is fine.
//   - Stryker has native Vitest integration; no fresh-subprocess-
//     per-mutant equivalent needed (Python's cryptography PyO3
//     binding was the reason for that on the cosmic-ray side).
//   - The mutation-runtime-vs-test-suite-runtime ratio is wildly
//     different: Python's full suite is ~17 min, JS's is ~1.2s.
//     A Stryker run over the same fileset finishes in minutes,
//     not tens of hours.
//
// Scope is the security-leverage subset:
//   - sender/form.js: compose flow, drives POST /api/secrets with
//     plaintext + passphrase + analytics-consent gates.
//   - reveal.js: receive flow, displays plaintext / image to the
//     recipient on first read.
//   - login.js: auth flow, TOTP / recovery-code state machine.
// Vendored swagger assets are excluded by default (not in `mutate`).
// The narrower scope keeps this PR a tractable first pass; follow-up
// PRs can expand to sender/form helpers (hints.js, dropzone.js,
// status-poll.js, tracked-list.js, url-cache.js) as the workflow's
// CI budget allows.

export default {
  packageManager: 'npm',
  testRunner: 'vitest',
  reporters: ['html', 'clear-text', 'progress', 'json'],
  // Stryker copies the whole project to a sandbox dir for each run.
  // Skip non-JS surfaces explicitly: the Python venv has symlinks
  // (lib64 -> lib) that break Stryker's copy step, and large dirs
  // like the Python test suite, audit/, and node-only build outputs
  // would just slow the sandbox setup. .gitignore does NOT cover
  // every entry here -- venv/ is gitignored, but stuff like
  // tests-e2e/ and tests/ aren't, and Stryker's defaults read
  // .gitignore but don't extend it. List explicitly.
  ignorePatterns: [
    'venv/',
    '.venv/',
    'env/',
    'tests/',
    'tests-e2e/',
    'audit/',
    'docs/',
    'scripts/',
    '.git/',
    'reports/',
    '.stryker-tmp/',
    'coverage/',
    'htmlcov/',
    '__pycache__/',
    '*.py',
    '*.db*',
    '*.log',
  ],
  // perTest coverage analysis lets Stryker skip running tests that
  // don't cover the mutated line -- a substantial speed-up on JS where
  // each module's test file is the dominant coverer of that module.
  coverageAnalysis: 'perTest',
  mutate: [
    'app/static/sender/form.js',
    'app/static/reveal.js',
    'app/static/login.js',
  ],
  // Stryker's default timeout is conservative; raise it to absorb the
  // jsdom environment startup that runs once per spawned worker.
  // Per-mutant timeout, NOT total runtime cap.
  timeoutMS: 30_000,
  // Concurrency: Stryker spawns N workers in parallel. The CI runner
  // has 2 vCPU; 2 workers fully utilizes it. Local interactive runs
  // can override via `--concurrency`.
  concurrency: 2,
  // Threshold for the report's pass/fail bucket. The actual gate (if
  // we add one) lives in the workflow file. These bands match the
  // cosmic-ray spec: "high" passing, "low" failing, "break" hard fail.
  thresholds: { high: 80, low: 60, break: 0 },
  // Where mutation reports land. The workflow uploads this directory
  // as a CI artifact for inspection.
  htmlReporter: { fileName: 'reports/mutation/index.html' },
  jsonReporter: { fileName: 'reports/mutation/mutation.json' },
  // Quiet down Stryker's own log noise; per-mutant failures still
  // surface through the test runner's stderr.
  logLevel: 'info',
};
