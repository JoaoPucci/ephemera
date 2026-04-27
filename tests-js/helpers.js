// Shared helpers for jsdom-based unit tests.
//
// The frontend JS files are now ES modules. Each test:
//   1. sets up a DOM fixture in `beforeEach`
//   2. calls `vi.resetModules()` to clear the module cache
//   3. `await import('../app/static/<entry>.js')` re-evaluates the module
//      top-level code (its listener wiring) against the fresh fixture
//
// `vi.resetModules()` is required because Vitest caches modules between
// tests by default; without it, the second test would see the wiring from
// the first test's DOM (long since discarded).
//
// `installI18nStub` is called inside `loadModule` so every loaded entry
// sees a functioning window.i18n before its top-level code runs. The stub
// mirrors the real shim in app/static/i18n.js (same dotted-key walk, same
// {{var}} interpolation, same "return the key on miss" sentinel) sourced
// from the real en.json -- so tests exercise the actual English strings
// the app ships with, not mock strings that could drift.

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const EN_CATALOG = JSON.parse(
  readFileSync(join(__dirname, '..', 'app', 'static', 'i18n', 'en.json'), 'utf-8')
);

function catalogLookup(tree, key) {
  let cur = tree;
  for (const seg of key.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[seg];
  }
  return typeof cur === 'string' ? cur : undefined;
}

function interpolate(template, vars) {
  if (!vars) return template;
  return template.replace(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g, (m, name) =>
    name in vars ? String(vars[name]) : m
  );
}

// Minimal replica of what app/static/i18n.js exposes on window.i18n. We don't
// exercise setLocale() from unit tests (it reloads the page, which jsdom
// doesn't do), so this only needs `t` + `currentLocale` to keep handlers
// happy.
export function installI18nStub() {
  const stub = {
    t(key, vars) {
      const hit = catalogLookup(EN_CATALOG, key);
      if (hit === undefined) return key; // matches real shim's visible-sentinel behavior
      return interpolate(hit, vars);
    },
    currentLocale: 'en',
    setLocale() {
      /* no-op in tests */
    },
  };
  // Both assignments: app code reads `window.i18n`; some test helpers look at
  // globalThis. In jsdom both resolve to the same object, but being explicit
  // about the contract future-proofs the stub against any env quirk.
  window.i18n = stub;
  globalThis.i18n = stub;
  return stub;
}

// Wait for all currently queued microtasks + a macrotask tick. Sufficient to
// drain "await fetch(...)" + a JSON parse + DOM writes in the handlers we test.
export function flushAsync() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

// Build a Response-like object for fetch stubs. jsdom ships a global Response
// so we just use it.
export function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

// A fetch mock that hangs forever -- useful for asserting "only one request
// was fired" because the first promise never resolves, so the handler stays
// stuck in its in-flight state.
export function neverResolveFetch() {
  return () => new Promise(() => {});
}

// Load a static-dir ES module fresh (clears Vitest's cache first, so each
// test gets the module's top-level wiring re-run against the current DOM).
// Usage:
//   await loadModule('login')                  -> ../app/static/login.js
//   await loadModule('sender/tracked-list')    -> ../app/static/sender/tracked-list.js
// Pass the bare name, no extension.
//
// Vite's dynamic-import analyzer only narrows a variable one path segment
// deep, so submodule loads route through their own template literal.
// Add a new branch here when a new app/static/<dir>/ module gains its
// own test suite.
//
// `installI18nStub()` is called before the import so handlers that reach
// `window.i18n.t(...)` during their top-level wiring or later inside a
// submit event don't blow up on an undefined global.
export async function loadModule(name) {
  installI18nStub();
  vi.resetModules();
  if (name.startsWith('sender/')) {
    const leaf = name.slice('sender/'.length);
    return await import(`../app/static/sender/${leaf}.js`);
  }
  return await import(`../app/static/${name}.js`);
}
