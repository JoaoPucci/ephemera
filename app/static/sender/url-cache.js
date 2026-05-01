// Client-side URL cache (localStorage).
//
// The URL returned by POST /api/secrets is `/s/{token}#{client_half}`. The
// server cannot reconstruct the fragment because it never sees it -- that's
// the whole point of key splitting. So if the user wants to re-copy a
// previously-issued URL from their tracked list, we have to cache it in the
// browser that created it. Keyed by the server-issued UUID, which is stable
// and present in every /api/secrets/tracked item.
//
// Two prototype-related hardenings on the storage object:
//
// 1. Lookups go through `Object.hasOwn` rather than truthy-coalesce
//    (`m[id] || null`). A plain `JSON.parse('{}')`-rooted object
//    inherits Object.prototype, so for any prototype-shadowed key
//    (`toString`, `hasOwnProperty`, `valueOf`, ...) `m[id]` reads
//    back the inherited function reference even if the cache never
//    set that key.
//
// 2. Writes go through `Object.defineProperty` rather than bracket
//    assignment (`m[id] = url`). Bracket-set on the special key
//    `__proto__` invokes prototype-mutation semantics instead of
//    creating an own data property, so `cacheUrl("__proto__", url)`
//    via bracket would silently not cache anything (and
//    `Object.hasOwn(m, "__proto__")` then returns false on the next
//    read). `defineProperty` always writes an own data property
//    regardless of the key. JSON.parse and JSON.stringify both
//    handle `__proto__` correctly when it's an own data property,
//    so the round-trip through localStorage is unaffected.
//
// Production ids are server-issued UUIDs so neither collision should
// happen in practice -- this is a structural belt-and-braces, not a
// fix for an observed bug.

const URL_STORE_KEY = 'ephemera_urls_v1';

function loadUrls() {
  try {
    return JSON.parse(localStorage.getItem(URL_STORE_KEY) || '{}');
  } catch {
    return {};
  }
}

function saveUrls(obj) {
  try {
    localStorage.setItem(URL_STORE_KEY, JSON.stringify(obj));
  } catch {}
}

export function cacheUrl(id, url) {
  const m = loadUrls();
  Object.defineProperty(m, id, {
    value: url,
    writable: true,
    enumerable: true,
    configurable: true,
  });
  saveUrls(m);
}

export function forgetUrl(id) {
  const m = loadUrls();
  if (Object.hasOwn(m, id)) {
    delete m[id];
    saveUrls(m);
  }
}

export function getUrl(id) {
  const m = loadUrls();
  if (!Object.hasOwn(m, id)) return null;
  const v = m[id];
  return typeof v === 'string' && v ? v : null;
}

// Drop any cached entries the server no longer has (expired, canceled, or
// untracked). Called with the list of ids the server just returned.
export function gcUrls(knownIds) {
  const m = loadUrls();
  const known = new Set(knownIds);
  let changed = false;
  for (const id of Object.keys(m)) {
    if (!known.has(id)) {
      delete m[id];
      changed = true;
    }
  }
  if (changed) saveUrls(m);
}
