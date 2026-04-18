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

import { vi } from 'vitest';

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
// Usage: `await loadModule('login')` — pass the bare name, no extension.
// The .js extension lives inside the template's static prefix so Vite's
// dynamic-import analyzer can narrow the candidate set to *.js files.
export async function loadModule(name) {
  vi.resetModules();
  return await import(`../app/static/${name}.js`);
}
