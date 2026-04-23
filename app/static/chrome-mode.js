// Prototype: switch the top-chrome presentation between variants for
// side-by-side UX comparison on narrow viewports.
//
//   ?chrome=hamburger  -- single menu button that expands to a drawer
//   ?chrome=shrink     -- in-place shrink (dot-only user pill, compact picker)
//   (no param)         -- current behavior (no responsive transform)
//
// The mode only activates under the mobile breakpoint (480px); above it,
// the chrome row stays as designed. Persisted to sessionStorage so the
// reload from a locale change keeps the same variant without re-appending
// the query string. Query param always wins over stored value.
(() => {
  const KEY = 'ephemera_chrome_mode_v1';
  const VALID = new Set(['hamburger', 'shrink']);

  const fromQuery = new URL(location.href).searchParams.get('chrome');
  const stored = sessionStorage.getItem(KEY);
  let mode = VALID.has(fromQuery) ? fromQuery : (VALID.has(stored) ? stored : null);

  if (fromQuery && VALID.has(fromQuery) && fromQuery !== stored) {
    sessionStorage.setItem(KEY, fromQuery);
  }

  if (mode) {
    // Set on the element that already exists at head-parse time.
    document.documentElement.dataset.chromeMode = mode;
  }
})();
