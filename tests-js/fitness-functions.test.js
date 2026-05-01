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

// Strip `/* ... */` block comments while preserving every original
// character position: comment chars become spaces, newlines stay.
// Length-preserving keeps `path:line` offender reports accurate when
// a regex match crosses lines.
//
// Walks character-by-character with simple state-tracking so the
// stripper doesn't mistake `/*` / `*/` sequences inside string or
// template literals for real comment delimiters. Without this, code
// like `const x = "/*"; console.log("secret"); const y = "*/";` would
// have the middle line erased (the regex sees one giant block comment
// from the first `/*` literal to the trailing `*/` literal), letting
// banned patterns evade every fitness check via valid JavaScript.
//
// Tracked contexts:
//   - Code: block comments stripped here.
//   - Line comments (`// ...` to newline): preserved as-is so a
//     comment containing `/*` or `*/` doesn't trip the stripper.
//     (Line comments themselves are not stripped -- the earlier
//     `(^|[^:])` line-comment stripper ate `//` in protocol-relative
//     URL strings, which we now intentionally allow to reach the
//     fetch-test regex.)
//   - String literals (`'...'`, `"..."`): contents preserved, walked
//     with backslash-escape handling. Unterminated strings end at
//     newline (matches JS's actual behavior; protects against runaway
//     consumption on a malformed file).
//   - Template literals (`` `...` ``): contents preserved, with simple
//     `${...}` depth tracking. Code inside `${}` interpolations is
//     NOT recursively scanned for block comments -- a documented
//     limitation, since recursing would require a real parser.
//   - Regex literals (`/.../flags`): NOT explicitly tracked. The
//     realistic shapes in this codebase (i18n placeholder, alphanum
//     filter, fragment anchor) don't contain `/*` or `*/`. A future
//     regex literal embedding those delimiters would be mis-handled;
//     if one is added, this scanner needs a regex-state branch.
function stripBlockComments(source) {
  const out = [];
  const n = source.length;
  let i = 0;
  while (i < n) {
    const ch = source[i];
    const nx = i + 1 < n ? source[i + 1] : '';

    if (ch === '/' && nx === '*') {
      const end = source.indexOf('*/', i + 2);
      if (end === -1) {
        // Unterminated -- pad the tail with spaces, keep newlines.
        for (let j = i; j < n; j++) out.push(source[j] === '\n' ? '\n' : ' ');
        return out.join('');
      }
      for (let j = i; j <= end + 1; j++) out.push(source[j] === '\n' ? '\n' : ' ');
      i = end + 2;
      continue;
    }

    if (ch === '/' && nx === '/') {
      while (i < n && source[i] !== '\n') {
        out.push(source[i]);
        i++;
      }
      continue;
    }

    if (ch === "'" || ch === '"') {
      const quote = ch;
      out.push(ch);
      i++;
      while (i < n) {
        const c = source[i];
        if (c === '\\' && i + 1 < n) {
          out.push(c, source[i + 1]);
          i += 2;
          continue;
        }
        out.push(c);
        i++;
        if (c === quote || c === '\n') break;
      }
      continue;
    }

    if (ch === '`') {
      out.push(ch);
      i++;
      let depth = 0;
      while (i < n) {
        const c = source[i];
        if (c === '\\' && i + 1 < n) {
          out.push(c, source[i + 1]);
          i += 2;
          continue;
        }
        if (c === '$' && i + 1 < n && source[i + 1] === '{') {
          out.push(c, '{');
          i += 2;
          depth++;
          continue;
        }
        if (c === '}' && depth > 0) {
          out.push(c);
          i++;
          depth--;
          continue;
        }
        out.push(c);
        i++;
        if (c === '`' && depth === 0) break;
      }
      continue;
    }

    out.push(ch);
    i++;
  }
  return out.join('');
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
