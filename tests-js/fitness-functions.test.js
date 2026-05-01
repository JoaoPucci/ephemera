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

// Strip block + line comments before regex matching. The line-comment
// regex uses `(^|[^:])` so URL strings ('https://...') keep their `//`.
// JS string and template literals are NOT stripped, but the patterns
// this file matches (`localStorage.setItem`, `eval(`, etc.) don't appear
// inside string literals in the production code; if a future change puts
// one of these patterns in a string literal, the right move is to
// refactor that string rather than relax the test.
function stripComments(source) {
  let out = source.replace(/\/\*[\s\S]*?\*\//g, '');
  out = out.replace(/(^|[^:])\/\/.*$/gm, '$1');
  return out;
}

function relPath(file) {
  return relative(REPO_ROOT, file);
}

// Walk JS_FILES (optionally minus an allowlist of relative paths), apply
// `predicate` to each (1-indexed) line of the comment-stripped source,
// and collect `path:line: text` triplets where the predicate fires.
function findOffenders(predicate, allowlist = new Set()) {
  const offenders = [];
  for (const file of JS_FILES) {
    const rel = relPath(file);
    if (allowlist.has(rel)) continue;
    const text = stripComments(readFileSync(file, 'utf8'));
    const lines = text.split('\n');
    for (let i = 0; i < lines.length; i++) {
      if (predicate(lines[i])) {
        offenders.push(`${rel}:${i + 1}: ${lines[i].trim()}`);
      }
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
    const banned = [
      /\beval\s*\(/,
      /\bnew\s+Function\s*\(/,
      /\bsetTimeout\s*\(\s*['"`]/,
      /\bsetInterval\s*\(\s*['"`]/,
    ];
    const offenders = findOffenders((line) => banned.some((re) => re.test(line)));
    expect(offenders, `Anti-RCE violations:\n  ${offenders.join('\n  ')}`).toEqual([]);
  });

  // -------------------------------------------------------------------
  // 2. No console.* outside the narrow operator-debug allowlist
  // -------------------------------------------------------------------
  it('forbids console.* outside the operator-debug allowlist', () => {
    // `console.log` / `console.error` / etc. land in browser devtools
    // and, depending on the user's setup, in any extension that taps
    // the console. The sender / reveal flow handles plaintext
    // (passphrase, content); a stray `console.log` there pours
    // secrets into a surface the user can't audit.
    //
    // Allowlist: app/static/two-click.js carries one legitimate
    // `console.error` for a programming error inside the
    // two-click-confirm helper's onConfirm callback -- the kind of
    // bug a developer needs to see, not a runtime user-facing signal.
    // Any other file adding a `console.*` call should explain why and
    // add itself to the allowlist below, rather than landing the call
    // by reflex.
    const allowlist = new Set(['app/static/two-click.js']);
    const offenders = findOffenders((line) => /\bconsole\s*\.\s*\w+\s*\(/.test(line), allowlist);
    expect(offenders, `Non-allowlisted console.* calls:\n  ${offenders.join('\n  ')}`).toEqual([]);
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
    const offenders = findOffenders(
      (line) => /\b(local|session)Storage\s*\.\s*\w+\s*\(/.test(line),
      allowlist
    );
    expect(
      offenders,
      `Non-allowlisted (local|session)Storage calls:\n  ${offenders.join('\n  ')}`
    ).toEqual([]);
  });

  // -------------------------------------------------------------------
  // 4. fetch() URLs are same-origin
  // -------------------------------------------------------------------
  it('forbids absolute http(s) URLs in fetch()', () => {
    // The application is single-origin by deployment -- every API
    // route lives at /api/..., /send/..., /s/..., or /static/...
    // relative to the page. An absolute http(s) URL passed to fetch()
    // would either be a configuration leak (a hard-coded staging /
    // production hostname) or a cross-origin call that could leak
    // the user's session cookie or the secret URL fragment.
    //
    // The CSP also has `connect-src 'self'`, so an absolute URL would
    // fail at runtime; this test catches the regression at source
    // before a release ships and the browser silently drops the call.
    const offenders = findOffenders((line) => /\bfetch\s*\(\s*['"`]https?:\/\//.test(line));
    expect(
      offenders,
      `Absolute-URL fetch calls (should be relative paths):\n  ${offenders.join('\n  ')}`
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
    // The pattern `[+\-*/%&|^]?=(?!=)` matches `=`, `+=`, `-=`, etc.
    // but excludes equality comparisons (`==`, `===`).
    const allowlist = new Set(['app/static/sender/tracked-list.js']);
    const offenders = findOffenders(
      (line) => /\.(inner|outer)HTML\b\s*[+\-*/%&|^]?=(?!=)/.test(line),
      allowlist
    );
    expect(
      offenders,
      `Non-allowlisted innerHTML/outerHTML assignments:\n  ${offenders.join('\n  ')}`
    ).toEqual([]);
  });
});
