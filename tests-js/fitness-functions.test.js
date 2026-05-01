// Architectural fitness functions for app/static/*.js.
//
// Mirror of tests/test_fitness_functions.py on the Python side: each
// test pins an invariant the codebase already upholds, derived from
// documented evidence (AGENTS.md, source comments, the way modules
// are structured today). The point is to catch regressions in code
// paths that runtime tests don't exercise -- a new file that adds
// `console.log(secret)` or `localStorage.setItem("plaintext", ...)`
// fails this suite at source rather than slipping into a release.
//
// Static-walk semantics: these tests don't load any production JS;
// they read it. The jsdom environment Vitest provides is unused here
// but cheap to inherit, so we don't override it for this file.

import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');
const STATIC_DIR = resolve(REPO_ROOT, 'app/static');

// Vendored Swagger UI assets are pinned third-party code -- their patterns
// (eval, innerHTML, etc.) are out of our hands and irrelevant to ephemera's
// invariants. The biome scan already excludes this directory; we mirror that.
const EXCLUDED_DIRS = new Set(['swagger']);

function findJsFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (EXCLUDED_DIRS.has(entry.name)) continue;
      out.push(...findJsFiles(join(dir, entry.name)));
    } else if (entry.isFile() && entry.name.endsWith('.js')) {
      out.push(join(dir, entry.name));
    }
  }
  return out.sort();
}

const JS_FILES = findJsFiles(STATIC_DIR);

// Strip block comments (`/* ... */`) before regex matching, while
// preserving every original character position: comment characters
// become spaces, newlines inside the block are kept. This means the
// stripped text has the same length and the same line numbers as the
// source -- crucial for accurate `path:line` offender reports when
// the regex match crosses lines.
//
// Line comments (`// ...`) are intentionally NOT stripped. The
// "obvious" stripper would treat `'//evil.example/x'` inside a string
// as a comment (because the `//` is preceded by `'`, not `:`) and erase
// it -- which is exactly the protocol-relative URL the same-origin
// fitness check is supposed to catch. Robust line-comment detection
// would need a real JS lexer; the pragmatic alternative is to leave
// line comments in. Verified against the current source: the only
// line comment in production JS that mentions a risky pattern is
// two-click.js:115 ("Log to console.error..."), and the regex
// requires `console.<method>(` with an open paren, which the comment
// doesn't have. New comments that DO contain full call expressions
// would be flagged -- which is arguably correct: a comment saying
// "we removed `console.log(secret)` here" should ideally not survive
// in source.
//
// JS string and template literals are also not stripped, but the
// patterns this file matches don't appear inside string literals in
// production code today; if a future change puts one inside a string,
// the right move is to refactor that string rather than relax the test.
function stripBlockComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, (match) => match.replace(/[^\n]/g, ' '));
}

function relPath(file) {
  return relative(REPO_ROOT, file);
}

// Walk JS_FILES (optionally minus an allowlist of relative paths), apply
// `regex` (which MUST have the global flag) to the comment-stripped
// source AS A WHOLE -- not line-by-line -- so a banned pattern split
// across lines (`fetch(\n  "https://...")`, `localStorage\n.setItem`,
// chained `console\n  .log(...)`) still matches. For each match, report
// the file path, the line number where the match starts, and the text
// of that starting line trimmed.
function findOffenders(regex, allowlist = new Set()) {
  if (!regex.global) {
    throw new Error('findOffenders: regex must have the global flag');
  }
  const offenders = [];
  for (const file of JS_FILES) {
    const rel = relPath(file);
    if (allowlist.has(rel)) continue;
    const text = stripBlockComments(readFileSync(file, 'utf8'));
    for (const match of text.matchAll(regex)) {
      const before = text.slice(0, match.index);
      const lineNumber = before.split('\n').length;
      const lineStart = before.lastIndexOf('\n') + 1;
      const lineEndIdx = text.indexOf('\n', match.index);
      const lineEnd = lineEndIdx === -1 ? text.length : lineEndIdx;
      const lineText = text.slice(lineStart, lineEnd).trim();
      offenders.push(`${rel}:${lineNumber}: ${lineText}`);
    }
  }
  return offenders;
}

describe('JS architectural fitness functions', () => {
  // -------------------------------------------------------------------
  // 1. Anti-RCE: no eval, no new Function, no string-arg setTimeout/setInterval
  // -------------------------------------------------------------------
  it('forbids eval, new Function, and string-arg setTimeout/setInterval', () => {
    // Why: each of these takes a string and runs it as code.
    //  - `eval(s)` / `new Function(s)`: arbitrary code from any string
    //    value reachable at the call site, bypassing the CSP's
    //    `script-src 'self'` (the policy applies to <script> tags, not
    //    to JS calling its own runtime).
    //  - `setTimeout("foo()", n)` / `setInterval("foo()", n)`: the
    //    string overload delegates to eval too. Function references
    //    are fine; only the string-typed first argument is forbidden.
    const banned =
      /\beval\s*\(|\bnew\s+Function\s*\(|\bsetTimeout\s*\(\s*['"`]|\bsetInterval\s*\(\s*['"`]/g;
    const offenders = findOffenders(banned);
    expect(offenders, `Anti-RCE violations:\n  ${offenders.join('\n  ')}`).toEqual([]);
  });

  // -------------------------------------------------------------------
  // 2. No console.* outside the narrow operator-debug allowlist
  // -------------------------------------------------------------------
  it('caps console.* calls per file at the documented count', () => {
    // `console.log` / `console.error` / etc. land in browser devtools
    // and, depending on the user's setup, in any extension that taps
    // the console. The sender / reveal flow handles plaintext
    // (passphrase, content); a stray `console.log` there pours
    // secrets into a surface the user can't audit.
    //
    // Default per-file budget is zero. The single documented
    // exception is app/static/two-click.js, which carries one
    // legitimate `console.error` for a programming error inside the
    // two-click-confirm helper's onConfirm callback -- a developer
    // signal, not user-facing. The check pins that file's budget at
    // EXACTLY 1: a future second `console.log(secret)` added in the
    // same file would push the count to 2 and trip this test, even
    // though the file is "allowlisted." Adjusting the budget is a
    // deliberate decision with diff-visibility, not a free pass.
    //
    // Adding a console.* anywhere else, or adding a second one in
    // two-click.js, fails this test. The fix is either (a) remove
    // the call, or (b) update the budget here with a one-line
    // rationale for the new allowance.
    const expectedConsoleCalls = new Map([['app/static/two-click.js', 1]]);
    const offenders = [];
    for (const file of JS_FILES) {
      const rel = relPath(file);
      const text = stripBlockComments(readFileSync(file, 'utf8'));
      const count = [...text.matchAll(/\bconsole\s*\.\s*\w+\s*\(/g)].length;
      const expected = expectedConsoleCalls.get(rel) ?? 0;
      if (count !== expected) {
        offenders.push(`${rel}: ${count} console.* call(s), expected ${expected}`);
      }
    }
    expect(offenders, `console.* per-file budget mismatch:\n  ${offenders.join('\n  ')}`).toEqual(
      []
    );
  });

  // -------------------------------------------------------------------
  // 3. localStorage / sessionStorage only in documented persistence files
  // -------------------------------------------------------------------
  it('restricts (local|session)Storage calls to the persistence allowlist', () => {
    // localStorage is the only way a one-time-secret URL with the
    // client_half fragment can be re-shown to the original sender:
    // the server can't reconstruct the fragment, so
    // sender/url-cache.js saves it after creation. theme.js +
    // i18n.js use it for UI preferences -- nothing sensitive.
    //
    // Anywhere else, persisting via localStorage is a privacy risk:
    // the browser keeps it forever (until cleared), it's readable by
    // any extension, and it survives the page that produced it. A
    // future regression could land `localStorage.setItem("draft",
    // plaintext)` inside the compose form to "improve UX"; this test
    // makes that decision visible.
    //
    // sessionStorage is stricter (cleared on tab close) but enforced
    // identically here: same allowlist, same rationale. If a file
    // genuinely needs ambient persistence, add it to the allowlist
    // with a one-line note for the next reader.
    const allowlist = new Set([
      'app/static/theme.js',
      'app/static/i18n.js',
      'app/static/sender/url-cache.js',
    ]);
    const offenders = findOffenders(/\b(?:local|session)Storage\s*\.\s*\w+\s*\(/g, allowlist);
    expect(
      offenders,
      `Non-allowlisted (local|session)Storage calls:\n  ${offenders.join('\n  ')}`
    ).toEqual([]);
  });

  // -------------------------------------------------------------------
  // 4. fetch() URLs are same-origin
  // -------------------------------------------------------------------
  it('forbids absolute or protocol-relative URLs in fetch()', () => {
    // The application is single-origin by deployment -- every API
    // route lives at /api/..., /send/..., /s/..., or /static/...
    // relative to the page. An absolute URL passed to fetch() would
    // either be a configuration leak (a hard-coded staging /
    // production hostname) or a cross-origin call that could leak
    // the user's session cookie or the secret URL fragment.
    //
    // Three shapes are forbidden:
    //   fetch("https://...")    explicit https
    //   fetch("http://...")     explicit http (also a downgrade smell)
    //   fetch("//host/path")    protocol-relative -- inherits the
    //                            page's scheme but goes to a different
    //                            host, still cross-origin
    //
    // The CSP `connect-src 'self'` would block all three at runtime;
    // this test catches them at source so the regression doesn't
    // ship and the browser doesn't silently drop the call.
    //
    // Limitation: only literal URL arguments are detected. A variable
    // holding an absolute URL slips through. Catching every literal
    // is the high-leverage win; runtime CSP catches the rest.
    const offenders = findOffenders(/\bfetch\s*\(\s*['"`](?:https?:)?\/\//g);
    expect(
      offenders,
      `Absolute / protocol-relative fetch calls (should be relative paths):\n  ${offenders.join('\n  ')}`
    ).toEqual([]);
  });

  // -------------------------------------------------------------------
  // 5. innerHTML / outerHTML only in the documented clear-and-rebuild surface
  // -------------------------------------------------------------------
  it('restricts innerHTML / outerHTML assignment to the rebuild allowlist', () => {
    // `textContent` vs `innerHTML` is the canonical XSS-safe
    // distinction. If a value containing user-controlled bytes ever
    // reaches `el.innerHTML = value`, the browser parses it as HTML
    // -- any `<script>`, `<img onerror>`, etc. fires.
    //
    // Allowlist: app/static/sender/tracked-list.js does
    // `list.innerHTML = ''` to clear the tracked list before
    // re-rendering from server data. The right-hand side is the
    // literal empty string, not a value sourced from anywhere -- it
    // can't carry a payload. Any new call site should justify why
    // textContent / DOM construction doesn't fit, not assume
    // innerHTML is the fast path.
    //
    // The operator group covers every JS compound-assignment shape so
    // modern logical-assignment forms (`||=`, `&&=`, `??=`) and shift /
    // exponent compounds (`**=`, `<<=`, `>>=`, `>>>=`) can't bypass the
    // gate. The trailing `=(?!=)` still rejects equality comparisons
    // (`==`, `===`).
    const allowlist = new Set(['app/static/sender/tracked-list.js']);
    const offenders = findOffenders(
      /\.(?:inner|outer)HTML\b\s*(?:\*\*|<<|>>>?|&&|\|\||\?\?|[+\-*/%&|^])?=(?!=)/g,
      allowlist
    );
    expect(
      offenders,
      `Non-allowlisted innerHTML/outerHTML assignments:\n  ${offenders.join('\n  ')}`
    ).toEqual([]);
  });
});
