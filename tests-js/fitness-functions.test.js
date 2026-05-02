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

// Position metadata keys that the AST-walking helpers skip (acorn
// emits these on every node and they aren't part of the structural
// shape we care about). Hoisted here so `buildAliasMap` and the
// generic `walkAST` can both use it -- `buildAliasMap` runs at
// module-load time (PARSED initialiser), so `META_KEYS` must be
// declared before any function that closes over it.
const META_KEYS = new Set(['type', 'loc', 'start', 'end', 'range']);

// Pre-parse every JS file once, share the resulting ASTs across tests.
const PARSED = findJsFiles(STATIC_DIR).map((file) => {
  const ast = acornParse(readFileSync(file, 'utf8'), {
    ecmaVersion: 'latest',
    sourceType: 'module',
    allowHashBang: true,
    locations: true,
  });
  return { file, rel: relPath(file), ast, aliasMap: buildAliasMap(ast) };
});

// Walk the entire AST and build a Name -> Array<chain> map for every
// `const|let|var <name> = <chain>` declaration where `<chain>` is
// either an Identifier or a MemberExpression (the kinds we
// statically recognize as references to globals or methods on them).
// This catches the alias-bypass pattern at every scope:
//
//   const f = fetch;          aliasMap.set('f', [Identifier('fetch')])
//   const log = console.log;  aliasMap.set('log', [Member(console, log)])
//   function fn() {           aliasMap also catches function-local
//     const e = eval;         aliases like `const e = eval` so
//     e(payload);             `e(payload)` reaches the eval check
//   }
//
// Multiple bindings of the same name are appended to the array
// rather than overwriting. Without per-scope tracking, a single map
// can't tell whether top-level `const f = fetch` and inner `const f
// = safeFn` shadow each other or coexist; conservative direction
// for a fitness checker is to keep BOTH and let predicates OR-match
// over them. Concretely:
//
//   const f = safeFn;            // outer scope
//   function attack() {
//     const f = fetch;           // shadowing inner
//     f("https://evil");         // -> aliasMap['f'] = [safeFn, fetch]
//   }                            //    OR-match -> fetch reachable -> flag
//
// False-positive risk: a non-attacking inner `const f = fetch` plus
// an outer `const f = safeFn; f(...)` could be flagged on the outer
// call. For the realistic codebase the trade is fine -- aliases of
// dangerous globals are rare in benign code, and over-flagging is
// the safer side of a security-shape check. A precise scope-aware
// resolver would need symbol-table tracking for declarations,
// parameters, rebinds, and inner-function boundaries; out of scope.
// Synthesize a MemberExpression node `init.<propertyName>` for the
// alias map. The destructuring helper uses this when binding a name
// extracted from a destructured pattern: `const { fetch: f } = G`
// yields `f -> synthesizeMember(G, 'fetch')`, which downstream
// helpers (chainEndsWithName, resolvesToGlobalObject) can walk
// exactly like a hand-written `G.fetch` Member node.
function synthesizeMember(object, propertyName) {
  return {
    type: 'MemberExpression',
    object,
    property: { type: 'Identifier', name: propertyName },
    computed: false,
    optional: false,
  };
}

// Read the property name from one ObjectPattern property, or null
// if it isn't statically resolvable (computed keys with a non-string
// expression). Handles four key shapes:
//
//   { fetch: f }     bare identifier        -- prop.computed=false
//                                              prop.key=Identifier
//   { 'fetch': f }   quoted property name   -- prop.computed=false
//                                              prop.key=Literal
//   { ["fetch"]: f } computed string        -- prop.computed=true
//                                              prop.key=Literal
//   { [`fetch`]: f } computed template      -- prop.computed=true
//                                              prop.key=TemplateLiteral
//                                              (single quasi, no expr)
function patternPropertyName(prop) {
  if (!prop.computed && prop.key.type === 'Identifier') return prop.key.name;
  if (prop.key.type === 'Literal' && typeof prop.key.value === 'string') return prop.key.value;
  if (
    prop.computed &&
    prop.key.type === 'TemplateLiteral' &&
    prop.key.expressions.length === 0 &&
    prop.key.quasis.length === 1
  ) {
    return prop.key.quasis[0].value.cooked;
  }
  return null;
}

function buildAliasMap(ast) {
  // Inline walk (rather than calling `walkAST`) because this runs at
  // module-load time during the PARSED initialization, before
  // walkAST's `META_KEYS` const is in scope. Same shape, narrower
  // dependency.
  const map = new Map();

  function addAlias(name, init) {
    if (!init) return;
    // Two value-shape families recorded:
    //   - Chains: Identifier / MemberExpression / ChainExpression --
    //     resolve through to a global identifier for the URL / RCE /
    //     console / storage guards.
    //   - Strings: Literal (string) / TemplateLiteral -- exposed to
    //     `isStringShapedArg` and `staticStringPrefixes` so a `const
    //     code = "alert(1)"; setTimeout(code, 0)` indirection still
    //     reaches the timer guard, and an aliased URL string still
    //     reaches the absolute-URL fetch guard.
    // The chain-iterating predicates (chainEndsWithName,
    // resolvesToGlobalObject, isMethodCallOn) skip non-Identifier/
    // -Member candidates, so adding string-shaped values here doesn't
    // perturb their results.
    const isChain =
      init.type === 'Identifier' ||
      init.type === 'MemberExpression' ||
      init.type === 'ChainExpression';
    const isStringShape =
      (init.type === 'Literal' && typeof init.value === 'string') ||
      init.type === 'TemplateLiteral';
    if (!isChain && !isStringShape) return;
    const list = map.get(name);
    if (list) list.push(init);
    else map.set(name, [init]);
  }

  // Bind `name` (the identifier captured by one ObjectPattern /
  // ArrayPattern slot) to `init`. If the slot was a nested pattern,
  // recurse into it instead.
  function bindPatternSlot(value, init) {
    let v = value;
    if (v.type === 'AssignmentPattern') v = v.left;
    if (v.type === 'Identifier') {
      addAlias(v.name, init);
    } else if (v.type === 'ObjectPattern') {
      processObjectPattern(v, init);
    } else if (v.type === 'ArrayPattern') {
      processArrayPattern(v, init);
    }
  }

  // Walk an ObjectPattern (LHS of `const { ... } = init`) and
  // synthesize aliases for each named property. Recurses into
  // nested ObjectPatterns and ArrayPatterns and through
  // AssignmentPattern wrappers (the `= default` shape).
  function processObjectPattern(pattern, init) {
    if (
      !init ||
      (init.type !== 'Identifier' &&
        init.type !== 'MemberExpression' &&
        init.type !== 'ChainExpression')
    ) {
      return;
    }
    for (const prop of pattern.properties) {
      if (prop.type !== 'Property') continue;
      const keyName = patternPropertyName(prop);
      if (keyName === null) continue;
      bindPatternSlot(prop.value, synthesizeMember(init, keyName));
    }
  }

  // Walk an ArrayPattern (LHS of `const [...] = init`) and bind each
  // slot to the corresponding init element BY INDEX. Only resolves
  // when init is an ArrayExpression literal -- a non-literal init
  // (`const [f] = something()`) can't be statically paired by index.
  // RestElement (`const [a, ...rest] = ...`) and holes are skipped.
  function processArrayPattern(pattern, init) {
    if (!init || init.type !== 'ArrayExpression') return;
    for (let i = 0; i < pattern.elements.length; i++) {
      const slot = pattern.elements[i];
      if (!slot || slot.type === 'RestElement') continue;
      const initEl = init.elements[i];
      if (!initEl || initEl.type === 'SpreadElement') continue;
      bindPatternSlot(slot, initEl);
    }
  }

  function processVariableDeclaration(node) {
    for (const decl of node.declarations) {
      if (!decl.init) continue;
      if (decl.id?.type === 'Identifier') {
        addAlias(decl.id.name, decl.init);
      } else if (decl.id?.type === 'ObjectPattern') {
        processObjectPattern(decl.id, decl.init);
      } else if (decl.id?.type === 'ArrayPattern') {
        processArrayPattern(decl.id, decl.init);
      }
    }
  }

  function processAssignmentExpression(node) {
    if (node.operator !== '=') return;
    const left = node.left;
    if (!left) return;
    if (left.type === 'Identifier') {
      // Plain `f = fetch` after an earlier `let f` (or any later
      // rebinding). The let-with-no-init shape is invisible to the
      // VariableDeclaration arm above (decl.init is null), so we
      // pick the binding up here on the assignment expression.
      addAlias(left.name, node.right);
    } else if (left.type === 'ObjectPattern') {
      // `({ fetch: f } = globalThis)` -- destructuring assignment to
      // an existing binding. Same effect as `const { fetch: f } = ...`
      // for our purposes.
      processObjectPattern(left, node.right);
    } else if (left.type === 'ArrayPattern') {
      // `[f] = [fetch]` -- array-destructuring assignment.
      processArrayPattern(left, node.right);
    }
  }

  function processNode(node) {
    if (node.type === 'VariableDeclaration') {
      processVariableDeclaration(node);
    } else if (node.type === 'AssignmentExpression') {
      processAssignmentExpression(node);
    }
  }

  walkAST(ast, processNode);
  return map;
}

// Return an array of candidate chains that `node` can resolve to
// under the alias map. Always returns at least one element (the
// input itself, when no aliasing applies) so callers iterate
// uniformly. Walks through alias-of-alias chains, expanding every
// branch when a name has multiple bindings. Tracks visited names to
// bail on cycles, and caps depth at 8 per branch as a belt-and-
// braces guard.
function resolveAlias(node, aliasMap) {
  if (!aliasMap) return [node];
  const out = [];
  function walk(cur, depth, seen) {
    if (depth > 8) {
      out.push(cur);
      return;
    }
    const u = cur && cur.type === 'ChainExpression' ? cur.expression : cur;
    if (!u || u.type !== 'Identifier' || !aliasMap.has(u.name) || seen.has(u.name)) {
      out.push(cur);
      return;
    }
    const next = new Set(seen);
    next.add(u.name);
    for (const candidate of aliasMap.get(u.name)) walk(candidate, depth + 1, next);
  }
  walk(node, 0, new Set());
  return out.length > 0 ? out : [node];
}

// Generic AST walker. `visit(node, parent)` is called for every node;
// to skip a subtree, return `false` from the visitor. Recurses on every
// child that's either an array of nodes or a node-shaped object (one
// with a string `type`); skips position metadata keys (`loc`, `start`,
// `end`, `range`). `META_KEYS` is hoisted to the top of this file so
// `buildAliasMap` can reuse this same walker at module-load time.
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
//   - aliases at module scope: `const w = window; w.fetch(...)` -- the
//     `w` resolves through `aliasMap` back to `window`
// All the listed globals point at the same object on the browser side
// (`window === window.window === window.self === globalThis`), so any
// chain of them is equivalent to bare access for fitness purposes.
function resolvesToGlobalObject(node, aliasMap) {
  for (const candidate of resolveAlias(node, aliasMap)) {
    const u = unwrapSequence(unwrapChain(candidate));
    if (!u) continue;
    if (u.type === 'Identifier') {
      if (GLOBAL_OBJECTS.has(u.name)) return true;
      continue;
    }
    if (u.type !== 'MemberExpression') continue;
    const prop = memberPropertyName(u);
    if (prop === null || !GLOBAL_OBJECTS.has(prop)) continue;
    if (resolvesToGlobalObject(u.object, aliasMap)) return true;
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
// Module-level aliases also resolve through `aliasMap`: `const f =
// fetch; f(...)` and `const log = console.log; log(...)` reduce
// to the underlying chain via `resolveAlias`.
//
// Optional chaining at any layer is handled by `unwrapChain`; comma
// wrapping is handled by `unwrapSequence`.
function chainEndsWithName(node, targetName, aliasMap) {
  for (const candidate of resolveAlias(node, aliasMap)) {
    const u = unwrapSequence(unwrapChain(candidate));
    if (!u) continue;
    if (u.type === 'Identifier') {
      if (u.name === targetName) return true;
      continue;
    }
    if (u.type !== 'MemberExpression') continue;
    if (memberPropertyName(u) !== targetName) continue;
    if (resolvesToGlobalObject(u.object, aliasMap)) return true;
  }
  return false;
}

// Peel `Function.prototype.{call, apply, bind}` wrappers off a Call
// node and return the LOGICAL callee + arguments -- i.e., the
// function actually being invoked and the arguments it actually
// receives once `thisArg`-and-friends are stripped. Returns null if
// the input isn't a CallExpression.
//
// The four shapes peeled (each becomes equivalent to bare `fn(args)`
// for our predicates' purposes):
//
//   fn.call(thisArg, a, b)     -> callee=fn, args=[a, b]
//   fn.apply(thisArg, [a, b])  -> callee=fn, args=[a, b] (literal Array
//                                 only; non-literal `apply` args can't
//                                 be statically resolved -- args is
//                                 returned as null in that case)
//   fn.bind(thisArg, p1)(a)    -> callee=fn, args=[p1, a]
//   fn(a)                      -> callee=fn, args=[a]   (no wrapper)
//
// Without this peel, `fetch.call(window, "https://evil")`,
// `console.log.bind(console)(secret)`, `setTimeout.apply(window,
// ["code", 0])`, etc. all execute the same dangerous APIs but slip
// past the chain-ends-with predicates because the outer callee is
// `<fn>.{call, apply}` (a MemberExpression with the wrong final
// property name) or the entire bind() invocation result.
// Peel a single layer of `.bind(...)` from a value-position
// expression and return the bound function. Used when a peeled
// wrapper's resulting callee is itself a `.bind(...)` expression
// (composed wrappers like `fetch.bind(window).call(window, "x")`).
// Returns null if `node` isn't a bind call.
function unwrapBindValue(node) {
  if (!node || node.type !== 'CallExpression') return null;
  const callee = unwrapSequence(unwrapChain(node.callee));
  if (!callee || callee.type !== 'MemberExpression') return null;
  if (memberPropertyName(callee) !== 'bind') return null;
  return callee.object;
}

// If `newCallee` is itself a wrapped CallExpression, recurse into it
// to keep peeling. Returns the deepest underlying function, after
// also collapsing any trailing `.bind(...)` chain on the value.
//
// Two distinct shapes a Call here can take:
//
//   - Bind value: `fetch.bind(window)` -- a Call whose callee is
//     `<fn>.bind`. As a value, this evaluates to the bound function.
//     `effectiveCall` would describe this Call as a call TO the bind
//     method, returning `<fn>.bind` Member as the callee, which is
//     not what we want. `peelBindChain` already iterates
//     `unwrapBindValue` and reduces it to the underlying `<fn>`, so
//     try that first when the Call is a bind value.
//   - Wrapped Call: `Reflect.apply(...)`, `<fn>.call(...)`,
//     `<fn>.apply(...)`, `<fn>.bind(...)(args)` -- recurse via
//     `effectiveCall` to recover the logical callee, then collapse
//     any trailing bind chain.
//
// Without the bind-value branch, composed wrappers like
// `fetch.bind(window).call(window, "https://evil")` slip through:
// `peelCallOrApply` hands `fetch.bind(window)` (Call) to this
// helper, the recursive `effectiveCall` returns the `fetch.bind`
// Member, `peelBindChain` on a Member is a no-op, and
// `chainEndsWithName(..., 'fetch')` fails downstream.
function resolveWrappedCallee(newCallee, depth, aliasMap) {
  if (newCallee?.type === 'CallExpression') {
    const bindPeeled = peelBindChain(newCallee);
    if (bindPeeled !== newCallee) return bindPeeled;
    const inner = effectiveCall(newCallee, depth + 1, aliasMap);
    if (inner) return peelBindChain(inner.callee);
  }
  return peelBindChain(newCallee);
}

// Shape: Reflect.apply(fn, thisArg, argsArray)
//        Reflect.construct(fn, argsArray, newTarget?)
//
// The receiver match is via `chainEndsWithName(callee.object,
// 'Reflect', aliasMap)`, not a bare-Identifier check, so globally-
// qualified shapes like `globalThis.Reflect.apply(fetch, ...)` and
// `window.Reflect.construct(fetch, [...])` and any aliased binding
// (`const R = globalThis.Reflect; R.apply(...)`) unwrap the same way
// bare `Reflect.apply(...)` does.
function peelReflect(callee, node, depth, aliasMap) {
  if (!callee || callee.type !== 'MemberExpression' || !memberPropertyName(callee)) {
    return null;
  }
  if (!chainEndsWithName(callee.object, 'Reflect', aliasMap)) return null;
  const which = memberPropertyName(callee);
  if (which !== 'apply' && which !== 'construct') return null;
  const innerFn = node.arguments[0];
  const argsArrayIdx = which === 'apply' ? 2 : 1;
  const argsArray = node.arguments[argsArrayIdx];
  const args = argsArray?.type === 'ArrayExpression' ? argsArray.elements : null;
  return { callee: resolveWrappedCallee(innerFn, depth, aliasMap), args };
}

// Shape: fn.bind(thisArg, ...partials)(args...) -- outer callee is
// itself a CallExpression whose callee is `<fn>.bind`. The bound
// function is `<fn>`; logical args are `[...partials, ...outerArgs]`.
function peelImmediateBind(callee, node, depth, aliasMap) {
  if (!callee || callee.type !== 'CallExpression') return null;
  const innerCallee = unwrapSequence(unwrapChain(callee.callee));
  if (
    !innerCallee ||
    innerCallee.type !== 'MemberExpression' ||
    memberPropertyName(innerCallee) !== 'bind'
  ) {
    return null;
  }
  const partials = callee.arguments.slice(1);
  const newCallee = innerCallee.object;
  const newArgs = [...partials, ...node.arguments];
  return { callee: resolveWrappedCallee(newCallee, depth, aliasMap), args: newArgs };
}

// Shape: fn.call(thisArg, ...args) or fn.apply(thisArg, argsArray)
function peelCallOrApply(callee, node, depth, aliasMap) {
  if (!callee || callee.type !== 'MemberExpression') return null;
  const prop = memberPropertyName(callee);
  if (prop !== 'call' && prop !== 'apply') return null;
  const newCallee = callee.object;
  let newArgs;
  if (prop === 'call') {
    newArgs = node.arguments.slice(1);
  } else {
    const argsArray = node.arguments[1];
    newArgs = argsArray?.type === 'ArrayExpression' ? argsArray.elements : null;
  }
  return { callee: resolveWrappedCallee(newCallee, depth, aliasMap), args: newArgs };
}

function effectiveCall(node, depth = 0, aliasMap = null) {
  if (depth > 8) return null;
  if (!node) return null;
  // NewExpression: `new fn(args...)`. No .call/.apply/.bind wrappers
  // (you can't `new fn.call(...)`), so just pass the callee + args
  // through. This lets the URL guard inspect arguments to a
  // hypothetical `new fetch("https://...")`.
  if (node.type === 'NewExpression') {
    return { callee: node.callee, args: node.arguments };
  }
  if (node.type !== 'CallExpression') return null;
  const callee = unwrapSequence(unwrapChain(node.callee));

  // Try each wrapper shape in turn. The branches are mutually
  // exclusive at the AST level (Reflect call, immediate bind() call,
  // .call()/.apply() chain), so order doesn't matter for correctness;
  // first match wins.
  return (
    peelReflect(callee, node, depth, aliasMap) ||
    peelImmediateBind(callee, node, depth, aliasMap) ||
    peelCallOrApply(callee, node, depth, aliasMap) || {
      callee: node.callee,
      args: node.arguments,
    }
  );
}

// Apply `unwrapBindValue` repeatedly so a callee that's a chain of
// bind expressions (`fetch.bind(a).bind(b)...`) reduces to the
// underlying function. Caps at 8 hops to bail on cycles.
function peelBindChain(node) {
  let cur = node;
  for (let i = 0; i < 8; i++) {
    const peeled = unwrapBindValue(cur);
    if (!peeled) return cur;
    cur = peeled;
  }
  return cur;
}

// Predicate: `node` is a CallExpression whose LOGICAL callee (after
// peeling .call/.apply/.bind wrappers) is a MemberExpression whose
// RECEIVER chain ends at `objectName` (possibly through `window.` /
// `globalThis.` / `self.` of any depth). Catches `console.log(...)`,
// `console?.log(...)`, `console["log"](...)`, `console?.["log"]?.(...)`,
// `window.console.log(...)`, `console.log.call(console, x)`,
// `console.log.bind(console)(x)`, and their optional/bracket variants.
function isMethodCallOn(node, objectName, aliasMap) {
  if (node.type !== 'CallExpression') return false;
  const eff = effectiveCall(node, 0, aliasMap);
  if (!eff) return false;
  // Resolve aliasing on the eff callee BEFORE expecting a Member.
  // `const log = console.log; log(secret)` -- `eff.callee` is
  // Identifier('log'), but resolveAlias substitutes the original
  // `console.log` MemberExpression so the receiver-chain check
  // still fires. ANY-match: if any candidate binding is a Member on
  // `objectName`, the call is on that object.
  for (const candidate of resolveAlias(eff.callee, aliasMap)) {
    const callee = unwrapSequence(unwrapChain(candidate));
    if (!callee || callee.type !== 'MemberExpression') continue;
    if (chainEndsWithName(callee.object, objectName, aliasMap)) return true;
  }
  return false;
}

// Predicate: `node` is a Call (or NewExpression) whose LOGICAL callee
// resolves to the global identifier `name`. Catches `fn(...)`,
// `fn?.(...)`, `new fn(...)`, `window.fn(...)` / `self.fn(...)` etc.,
// AND the wrapped forms `fn.call(thisArg, ...)`, `fn.apply(thisArg,
// [...])`, `fn.bind(thisArg)(...)`. NewExpression doesn't go through
// `effectiveCall` because `new fn.call(...)` etc. aren't meaningful
// JS shapes.
function isBareCallOf(node, name, aliasMap) {
  if (node.type === 'NewExpression') return chainEndsWithName(node.callee, name, aliasMap);
  if (node.type !== 'CallExpression') return false;
  const eff = effectiveCall(node, 0, aliasMap);
  return Boolean(eff && chainEndsWithName(eff.callee, name, aliasMap));
}

const ABSOLUTE_URL_RE = /^(?:https?:)?\/\//;

// Yield each string-shaped value `node` can resolve to under the
// alias map: the input itself when `node` is already a Literal /
// TemplateLiteral, plus every string-shaped binding reached by
// walking aliased Identifiers. `buildAliasMap` records `const code
// = "alert(1)"` and `const url = \`https://x\`` alongside chain
// aliases, so this iterates the candidate set once.
function* stringShapedCandidates(node, aliasMap) {
  if (!node) return;
  if (node.type === 'Literal' && typeof node.value === 'string') {
    yield node;
    return;
  }
  if (node.type === 'TemplateLiteral') {
    yield node;
    return;
  }
  if (node.type === 'Identifier' && aliasMap) {
    for (const candidate of resolveAlias(node, aliasMap)) {
      if (candidate === node) continue;
      yield* stringShapedCandidates(candidate, aliasMap);
    }
  }
}

// True iff `node` resolves (directly or through a Literal-valued
// alias) to a static string-shaped expression. Both Literal strings
// and TemplateLiterals are dangerous as the first argument to the
// timer-string overloads (`setTimeout("code", n)` and
// `setTimeout(\`code\`, n)` both run their first arg through eval),
// and the indirection `const code = "..."; setTimeout(code, 0)`
// produces the same RCE shape -- the alias-aware lookup catches
// that case.
function isStringShapedArg(node, aliasMap) {
  for (const _ of stringShapedCandidates(node, aliasMap)) return true;
  return false;
}

// Yield the static prefix of EVERY string-shaped value `node` can
// resolve to. For a regular string Literal, the prefix is the value.
// For a TemplateLiteral, the prefix is the cooked text of the FIRST
// quasi (the span before any `${...}` interpolation). The URL guard
// iterates and tests each prefix against `ABSOLUTE_URL_RE` -- any
// reachable candidate matching the regex is treated as a hit, since
// the multi-binding alias map can record more than one binding per
// name (file-wide map across scopes), and a benign prefix returned
// first would otherwise mask a dangerous one.
//
// `\`https://evil.example/x\`` -- no interpolation, full URL is
// the cooked prefix, matches the absolute-URL regex.
// `\`/api/${id}\``             -- prefix `/api/`, doesn't match.
// `\`${HOST}/path\``           -- empty first quasi, no prefix.
function* staticStringPrefixes(node, aliasMap) {
  for (const candidate of stringShapedCandidates(node, aliasMap)) {
    if (candidate.type === 'Literal' && typeof candidate.value === 'string') {
      yield candidate.value;
    } else if (candidate.type === 'TemplateLiteral' && candidate.quasis.length > 0) {
      yield candidate.quasis[0].value.cooked;
    }
  }
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
    function checkAntiRceNode(rel, aliasMap, offenders, node) {
      if (isBareCallOf(node, 'eval', aliasMap) || isBareCallOf(node, 'Function', aliasMap)) {
        offenders.push(
          `${rel}:${node.loc.start.line}: ${node.type} of ${rootIdentifierName(node.callee)}`
        );
        return;
      }
      if (
        !isBareCallOf(node, 'setTimeout', aliasMap) &&
        !isBareCallOf(node, 'setInterval', aliasMap)
      ) {
        return;
      }
      // The "first argument" check must use the LOGICAL arg list --
      // after .call/.apply/.bind wrappers strip the thisArg.
      // `setTimeout.call(window, "code", 0)` has `node.arguments[0]
      // === Identifier("window")` but the actual first arg the
      // function receives is "code".
      const eff = effectiveCall(node, 0, aliasMap);
      if (eff?.args && isStringShapedArg(eff.args[0], aliasMap)) {
        offenders.push(
          `${rel}:${node.loc.start.line}: string-arg ${rootIdentifierName(node.callee)}`
        );
      }
    }

    const offenders = [];
    for (const { rel, ast, aliasMap } of PARSED) {
      walkAST(ast, (node) => checkAntiRceNode(rel, aliasMap, offenders, node));
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
    for (const { rel, ast, aliasMap } of PARSED) {
      let count = 0;
      walkAST(ast, (node) => {
        if (isMethodCallOn(node, 'console', aliasMap)) count++;
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
    for (const { rel, ast, aliasMap } of PARSED) {
      if (allowlist.has(rel)) continue;
      walkAST(ast, (node) => {
        if (
          isMethodCallOn(node, 'localStorage', aliasMap) ||
          isMethodCallOn(node, 'sessionStorage', aliasMap)
        ) {
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
    function checkFetchUrlNode(rel, aliasMap, offenders, node) {
      if (!isBareCallOf(node, 'fetch', aliasMap)) return;
      // Match both regular string literals and template literals;
      // for templates, the static prefix is the cooked text of the
      // first quasi (the part before any `${...}`). A template
      // whose first quasi starts with `https?://` or `//` is a
      // hard-coded absolute URL regardless of any later
      // interpolation, and gets flagged.
      //
      // Use the LOGICAL first arg via `effectiveCall` so wrapped
      // forms like `fetch.call(window, "https://evil")` and
      // `fetch.bind(window)("https://evil")` are checked against
      // the URL the function actually receives, not against the
      // thisArg/etc. that .call/.apply/.bind interpose.
      const eff = effectiveCall(node, 0, aliasMap);
      if (!eff?.args) return;
      // Iterate every string-shaped candidate the first arg can
      // resolve to. The multi-binding alias map can record more than
      // one binding per name across scopes; a benign prefix returned
      // first would otherwise mask a dangerous one.
      for (const prefix of staticStringPrefixes(eff.args[0], aliasMap)) {
        if (ABSOLUTE_URL_RE.test(prefix)) {
          offenders.push(
            `${rel}:${node.loc.start.line}: fetch(${JSON.stringify(prefix).slice(0, 80)})`
          );
          break;
        }
      }
    }

    const offenders = [];
    for (const { rel, ast, aliasMap } of PARSED) {
      walkAST(ast, (node) => checkFetchUrlNode(rel, aliasMap, offenders, node));
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
    function checkHtmlAssignNode(rel, offenders, node) {
      if (node.type !== 'AssignmentExpression') return;
      if (!node.left || node.left.type !== 'MemberExpression') return;
      const prop = memberPropertyName(node.left);
      if (prop !== 'innerHTML' && prop !== 'outerHTML') return;
      const shape = node.left.computed ? 'bracket' : 'dot';
      offenders.push(`${rel}:${node.loc.start.line}: ${prop} ${node.operator} (${shape})`);
    }

    const offenders = [];
    for (const { rel, ast } of PARSED) {
      if (allowlist.has(rel)) continue;
      walkAST(ast, (node) => checkHtmlAssignNode(rel, offenders, node));
    }
    expect(
      offenders,
      `Non-allowlisted innerHTML/outerHTML assignments:\n  ${offenders.join('\n  ')}`
    ).toEqual([]);
  });
});
