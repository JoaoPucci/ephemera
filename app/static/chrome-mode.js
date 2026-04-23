// Prototype: enable the mobile hamburger chrome via "?chrome=hamburger".
// The query string captures into sessionStorage so the locale-change
// reload preserves the mode without re-appending the param.
//
// Runs at head-parse time so <html data-chrome-mode> is set before
// first paint, avoiding a flash of the default desktop chrome.
(() => {
  const KEY = 'ephemera_chrome_mode_v1';
  const VALID = new Set(['hamburger']);

  const fromQuery = new URL(location.href).searchParams.get('chrome');
  const stored = sessionStorage.getItem(KEY);
  const mode = VALID.has(fromQuery) ? fromQuery
             : VALID.has(stored)   ? stored
             : null;

  if (fromQuery && VALID.has(fromQuery) && fromQuery !== stored) {
    sessionStorage.setItem(KEY, fromQuery);
  }

  if (mode) {
    document.documentElement.dataset.chromeMode = mode;
  }
})();
