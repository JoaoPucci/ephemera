// Client-side URL cache (localStorage).
//
// The URL returned by POST /api/secrets is `/s/{token}#{client_half}`. The
// server cannot reconstruct the fragment because it never sees it -- that's
// the whole point of key splitting. So if the user wants to re-copy a
// previously-issued URL from their tracked list, we have to cache it in the
// browser that created it. Keyed by the server-issued UUID, which is stable
// and present in every /api/secrets/tracked item.

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
  m[id] = url;
  saveUrls(m);
}

export function forgetUrl(id) {
  const m = loadUrls();
  if (m[id]) {
    delete m[id];
    saveUrls(m);
  }
}

export function getUrl(id) {
  return loadUrls()[id] || null;
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
