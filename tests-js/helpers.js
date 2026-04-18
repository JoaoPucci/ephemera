// Shared helpers for jsdom-based unit tests.
//
// The frontend JS files are IIFEs that attach listeners on load -- they're
// not ES modules and aren't exported. To test them we read the source, build
// a DOM fixture, then evaluate the source with `new Function(...)()` which
// runs the IIFE against our fixture.
//
// Each test uses a fresh DOM + fresh IIFE invocation (beforeEach), so
// listeners from a previous test don't bleed into the next one.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = path.join(__dirname, '..', 'app', 'static');

export function readStatic(name) {
  return readFileSync(path.join(STATIC_DIR, name), 'utf-8');
}

export function evalScript(source) {
  // eslint-disable-next-line no-new-func
  new Function(source)();
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
