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
// Static-walk semantics, AST-grounded:
//   Each fitness check parses every app/static/**/*.js file with
//   `acorn` (ESTree 2020) and walks the resulting AST for the shape
//   it forbids. The earlier regex-based approach kept losing ground
//   to syntactic variants -- optional chaining (`console?.log`),
//   bracket access (`localStorage["setItem"]`), bare `Function(...)`
//   without `new`, optional call (`fetch?.(...)`), bracket-property
//   assignment (`el["innerHTML"] = ...`). All of those reduce to
//   well-typed nodes in the AST; the regex treadmill is replaced by
//   a single visitor pass per invariant.
//
// The jsdom environment Vitest provides is unused here but cheap to
// inherit, so we don't override it for this file.

import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse as acornParse } from 'acorn';
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

function relPath(file) {
  return relative(REPO_ROOT, file);
}

// Pre-parse every JS file once, share the resulting ASTs across tests.
const PARSED = findJsFiles(STATIC_DIR).map((file) => ({
  file,
  rel: relPath(file),
  ast: acornParse(readFileSync(file, 'utf8'), {
    ecmaVersion: 'latest',
    sourceType: 'module',
    allowHashBang: true,
    locations: true,
  }),
}));

// Generic AST walker. `visit(node, parent)` is called for every node;
// to skip a subtree, return `false` from the visitor. Recurses on every
// child that's either an array of nodes or a node-shaped object (one
// with a string `type`); skips position metadata keys (`loc`, `start`,
// `end`, `range`).
const META_KEYS = new Set(['type', 'loc', 'start', 'end', 'range']);
function walkAST(node, visit, parent = null) {
  if (!node || typeof node !== 'object') return;
  if (visit(node, parent) === false) return;
  for (const key of Object.keys(node)) {
    if (META_KEYS.has(key)) continue;
    const child = node[key];
    if (Array.isArray(child)) {
      for (const c of child) walkAST(c, visit, node);
    } else if (child && typeof child === 'object' && typeof child.type === 'string') {
      walkAST(child, visit, node);
    }
  }
}

// Unwrap a `ChainExpression` (the ESTree 2020 wrapper for optional-chain
// expressions like `a?.b()`) so callers can match on the inner shape
// uniformly. Returns the input unchanged if it isn't a ChainExpression.
function unwrapChain(node) {
  return node && node.type === 'ChainExpression' ? node.expression : node;
}

// Walk a member-or-call chain back to the leftmost identifier and
// return its name. Handles `console.log`, `console?.log`,
// `console["log"]`, `localStorage?.setItem(...)`, etc. Returns null if
// the chain doesn't bottom out at a bare Identifier (e.g., `(svc).x`,
// `obj[expr].y`).
function rootIdentifierName(node) {
  let cur = unwrapChain(node);
  while (cur) {
    if (cur.type === 'Identifier') return cur.name;
    if (cur.type === 'MemberExpression') {
      cur = cur.object;
      continue;
    }
    if (cur.type === 'CallExpression' || cur.type === 'NewExpression') {
      cur = cur.callee;
      continue;
    }
    return null;
  }
  return null;
}

// Read the property name of a `MemberExpression` regardless of how
// it's spelled. Three shapes count:
//   obj.prop                computed=false, property is Identifier
//   obj["prop"]             computed=true,  property is string Literal
//   obj[`prop`]             computed=true,  property is TemplateLiteral
//                                            with a single quasi and
//                                            no `${}` interpolations
// Computed accesses with non-literal indexes (`obj[expr]`,
// `` obj[`pre${x}fix`] ``) return null -- their effective property
// name can't be statically pinned to one string.
function memberPropertyName(node) {
  if (!node || node.type !== 'MemberExpression') return null;
  if (!node.computed) {
    return node.property.type === 'Identifier' ? node.property.name : null;
  }
  if (node.property.type === 'Literal' && typeof node.property.value === 'string') {
    return node.property.value;
  }
  if (
    node.property.type === 'TemplateLiteral' &&
    node.property.expressions.length === 0 &&
    node.property.quasis.length === 1
  ) {
    return node.property.quasis[0].value.cooked;
  }
  return null;
}

// Names that count as "the global object" -- callees rooted at any of
// these are treated as if the dangerous global were referenced bare.
// `window` and `globalThis` are universal; `self` is the standard
// alias on workers and an explicit alias for `window` on the main
// thread (used in some isomorphic libraries). Without this set,
// `window.eval(...)`, `globalThis.fetch("https://...")`,
// `self.console.log(secret)`, `window.localStorage.setItem(...)`
// would all bypass the bare-name and method-on-name predicates.
const GLOBAL_OBJECTS = new Set(['window', 'globalThis', 'self']);

// Peel a `SequenceExpression` (the comma operator) by taking its last
// subexpression -- the comma's runtime value. Catches the indirect-call
// pattern `(0, eval)("...")` where the parens wrap a comma expression
// to reference `eval` without a member-access binding (which makes it
// "indirect eval" -- runs in the global scope under non-strict mode).
// Same trick works for any global: `(0, fetch)("https://...")`,
// `(0, setTimeout)("code", n)`, etc. Without this peel, every such
// call's callee parses as a `SequenceExpression` and slips past the
// chain-ends-with checks.
//
// The loop handles pathological nesting like `((0, 1), eval)` -- the
// outer comma returns the inner comma's value, which is `eval`.
function unwrapSequence(node) {
  let cur = node;
  while (cur && cur.type === 'SequenceExpression' && cur.expressions.length > 0) {
    cur = unwrapChain(cur.expressions[cur.expressions.length - 1]);
  }
  return cur;
}

// Resolve whether `node` ultimately refers to the global object
// itself (not a property of it). True for:
//   - bare `window` / `globalThis` / `self`
//   - any chain of those, e.g. `window.window`, `self.window.self`,
//     `globalThis.self.globalThis`
// All the listed globals point at the same object on the browser side
// (`window === window.window === window.self === globalThis`), so any
// chain of them is equivalent to bare access for fitness purposes.
function resolvesToGlobalObject(node) {
  let cur = unwrapSequence(unwrapChain(node));
  while (cur) {
    if (cur.type === 'Identifier') return GLOBAL_OBJECTS.has(cur.name);
    if (cur.type !== 'MemberExpression') return false;
    const prop = memberPropertyName(cur);
    if (prop === null || !GLOBAL_OBJECTS.has(prop)) return false;
    cur = unwrapSequence(unwrapChain(cur.object));
  }
  return false;
}

// Resolve whether `node` (a callee chain or a member-access object)
// ultimately refers to the global named `targetName`. True for:
//   - bare `targetName` -- Identifier(targetName)
//   - `<global-chain>.<targetName>` -- one Member step from anything
//     that resolves to the global object, with the property name
//     (dot or bracket-string form) matching `targetName`. The global
//     chain can be any depth: `window.fetch`, `self.fetch`,
//     `window.window.fetch`, `self.window.globalThis.fetch`, etc.
//   - `(side, effect, <one of the above>)` -- the indirect-call /
//     comma-operator wrapper that's used to call globals without a
//     binding context
// Optional chaining at any layer is handled by `unwrapChain`; comma
// wrapping is handled by `unwrapSequence`.
function chainEndsWithName(node, targetName) {
  const u = unwrapSequence(unwrapChain(node));
  if (!u) return false;
  if (u.type === 'Identifier') return u.name === targetName;
  if (u.type !== 'MemberExpression') return false;
  if (memberPropertyName(u) !== targetName) return false;
  return resolvesToGlobalObject(u.object);
}

// Predicate: `node` is a CallExpression whose callee, after unwrapping
// optional chains, is a MemberExpression whose RECEIVER chain ends at
// `objectName` (possibly through `window.` / `globalThis.`). Catches
// `console.log(...)`, `console?.log(...)`, `console["log"](...)`,
// `console?.["log"]?.(...)`, AND `window.console.log(...)` /
// `globalThis.console.log(...)` and their optional/bracket variants.
function isMethodCallOn(node, objectName) {
  if (node.type !== 'CallExpression') return false;
  const callee = unwrapChain(node.callee);
  if (!callee || callee.type !== 'MemberExpression') return false;
  return chainEndsWithName(callee.object, objectName);
}

// Predicate: `node` is a Call (or NewExpression) whose callee resolves
// to the global identifier `name`. Catches `fn(...)`, `fn?.(...)`,
// `new fn(...)`, AND `window.fn(...)` / `globalThis.fn(...)` and their
// optional/bracket variants. The latter forms execute the same global
// API as the bare call, so they're treated identically by the anti-RCE
// and fetch checks.
function isBareCallOf(node, name) {
  if (node.type !== 'CallExpression' && node.type !== 'NewExpression') return false;
  return chainEndsWithName(node.callee, name);
}

const ABSOLUTE_URL_RE = /^(?:https?:)?\/\//;

// True iff `node` is a static string-shaped expression -- either a
// regular string `Literal` or a `TemplateLiteral`. Both compile to a
// string at runtime and both are dangerous as arguments to the
// timer-string overloads (`setTimeout(\`code\`, n)` runs the cooked
// template through eval just like `setTimeout("code", n)` does).
function isStringShapedArg(node) {
  if (!node) return false;
  if (node.type === 'Literal' && typeof node.value === 'string') return true;
  return node.type === 'TemplateLiteral';
}

// Read the static prefix of a string-shaped argument. For a regular
// string literal, returns its value. For a template literal, returns
// the cooked text of the FIRST quasi -- the part before any `${...}`
// interpolation. Returns null if the node isn't string-shaped.
//
// The fetch URL guard uses this on argument 0: a template like
// `\`https://evil.example/x\`` has no interpolation, so the cooked
// prefix IS the full URL and matches the absolute-URL regex.
// `\`/api/${id}\`` starts with `/api/`, doesn't match, correctly
// stays out. `\`${HOST}/path\`` has an empty first quasi and falls
// through (not a literal absolute URL we can statically prove).
function staticStringPrefix(node) {
  if (!node) return null;
  if (node.type === 'Literal' && typeof node.value === 'string') return node.value;
  if (node.type === 'TemplateLiteral' && node.quasis.length > 0) {
    return node.quasis[0].value.cooked;
  }
  return null;
}

describe('JS architectural fitness functions', () => {
  // -------------------------------------------------------------------
  // 1. Anti-RCE: no eval, no Function/new Function, no string-arg
  //    setTimeout/setInterval
  // -------------------------------------------------------------------
  it('forbids eval, Function/new Function, and string-arg setTimeout/setInterval', () => {
    // Why each is forbidden:
    //  - `eval(s)` / `Function(s)` / `new Function(s)`: every shape
    //    compiles and runs a string as code, bypassing the CSP's
    //    `script-src 'self'` (the policy applies to <script> tags,
    //    not to JS calling its own runtime). Bare `Function(...)` is
    //    just as dangerous as `new Function(...)`; both end up at
    //    the same constructor.
    //  - `setTimeout("foo()", n)` / `setInterval("foo()", n)`: the
    //    string overload delegates to eval too. Function references
    //    are fine; only the string-typed first argument is forbidden.
    const offenders = [];
    for (const { rel, ast } of PARSED) {
      walkAST(ast, (node) => {
        if (isBareCallOf(node, 'eval') || isBareCallOf(node, 'Function')) {
          offenders.push(
            `${rel}:${node.loc.start.line}: ${node.type} of ${rootIdentifierName(node.callee)}`
          );
          return;
        }
        if (
          (isBareCallOf(node, 'setTimeout') || isBareCallOf(node, 'setInterval')) &&
          isStringShapedArg(node.arguments[0])
        ) {
          offenders.push(
            `${rel}:${node.loc.start.line}: string-arg ${rootIdentifierName(node.callee)}`
          );
        }
      });
    }
    expect(offenders, `Anti-RCE violations:\n  ${offenders.join('\n  ')}`).toEqual([]);
  });

  // -------------------------------------------------------------------
  // 2. console.* per-file budget
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
    // same file would push the count to 2 and trip this test.
    // Adjusting the budget is a deliberate decision with diff-
    // visibility, not a free pass.
    //
    // Counts every shape: `console.log(...)`, `console?.log(...)`,
    // `console["log"](...)`, `console?.["log"]?.(...)` -- whatever
    // unwraps to a method call on the `console` identifier.
    const expectedConsoleCalls = new Map([['app/static/two-click.js', 1]]);
    const offenders = [];
    for (const { rel, ast } of PARSED) {
      let count = 0;
      walkAST(ast, (node) => {
        if (isMethodCallOn(node, 'console')) count++;
      });
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
  // 3. (local|session)Storage allowlist
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
    // identically here. Catches `storage.foo(...)`, `storage?.foo(...)`,
    // `storage["foo"](...)` -- every shape that resolves to a method
    // call on either identifier.
    const allowlist = new Set([
      'app/static/theme.js',
      'app/static/i18n.js',
      'app/static/sender/url-cache.js',
    ]);
    const offenders = [];
    for (const { rel, ast } of PARSED) {
      if (allowlist.has(rel)) continue;
      walkAST(ast, (node) => {
        if (isMethodCallOn(node, 'localStorage') || isMethodCallOn(node, 'sessionStorage')) {
          const root = rootIdentifierName(unwrapChain(node.callee));
          offenders.push(`${rel}:${node.loc.start.line}: ${root}`);
        }
      });
    }
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
    // Three URL shapes are forbidden:
    //   "https://..."        explicit https
    //   "http://..."         explicit http (also a downgrade smell)
    //   "//host/path"        protocol-relative -- inherits the page's
    //                         scheme but goes to a different host,
    //                         still cross-origin
    //
    // Catches every call shape that performs a fetch: `fetch(...)`,
    // `fetch?.(...)` (optional call), and `new fetch(...)` (rare but
    // valid). The CSP `connect-src 'self'` would block these at
    // runtime; this test catches the regression at source so the
    // browser doesn't silently drop the request.
    //
    // Limitation: only literal URL arguments are detected. A variable
    // holding an absolute URL slips through. Catching every literal
    // is the high-leverage win; runtime CSP catches the rest.
    const offenders = [];
    for (const { rel, ast } of PARSED) {
      walkAST(ast, (node) => {
        if (!isBareCallOf(node, 'fetch')) return;
        // Match both regular string literals and template literals;
        // for templates, the static prefix is the cooked text of the
        // first quasi (the part before any `${...}`). A template
        // whose first quasi starts with `https?://` or `//` is a
        // hard-coded absolute URL regardless of any later
        // interpolation, and gets flagged.
        const prefix = staticStringPrefix(node.arguments[0]);
        if (prefix == null) return;
        if (ABSOLUTE_URL_RE.test(prefix)) {
          offenders.push(
            `${rel}:${node.loc.start.line}: fetch(${JSON.stringify(prefix).slice(0, 80)})`
          );
        }
      });
    }
    expect(
      offenders,
      `Absolute / protocol-relative fetch calls (should be relative paths):\n  ${offenders.join('\n  ')}`
    ).toEqual([]);
  });

  // -------------------------------------------------------------------
  // 5. innerHTML / outerHTML assignment allowlist
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
    // Catches every assignment shape: dot (`el.innerHTML = x`),
    // bracket (`el["innerHTML"] = x`), and every JS compound
    // operator (`+=`, `||=`, `&&=`, `??=`, `**=`, `<<=`, etc. -- all
    // surface as `AssignmentExpression` with the corresponding
    // `operator` field). Equality comparisons (`==`, `===`) are
    // `BinaryExpression` nodes, not `AssignmentExpression`, so they
    // don't trip the gate.
    const allowlist = new Set(['app/static/sender/tracked-list.js']);
    const offenders = [];
    for (const { rel, ast } of PARSED) {
      if (allowlist.has(rel)) continue;
      walkAST(ast, (node) => {
        if (node.type !== 'AssignmentExpression') return;
        if (!node.left || node.left.type !== 'MemberExpression') return;
        const prop = memberPropertyName(node.left);
        if (prop === 'innerHTML' || prop === 'outerHTML') {
          offenders.push(
            `${rel}:${node.loc.start.line}: ${prop} ${node.operator} (${node.left.computed ? 'bracket' : 'dot'})`
          );
        }
      });
    }
    expect(
      offenders,
      `Non-allowlisted innerHTML/outerHTML assignments:\n  ${offenders.join('\n  ')}`
    ).toEqual([]);
  });
});
