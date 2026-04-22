// Theme selection with persistence.
// Two explicit themes: "light", "dark". On first visit, matches the OS preference.
// Applied as early as possible (in <head>) to avoid flash of wrong theme.
(() => {
  const KEY = 'ephemera_theme_v1';

  function systemPref() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark' : 'light';
  }

  function current() {
    const saved = localStorage.getItem(KEY);
    return saved === 'light' || saved === 'dark' ? saved : systemPref();
  }

  function apply(theme) {
    document.documentElement.dataset.theme = theme;
  }

  function setAndPersist(theme) {
    localStorage.setItem(KEY, theme);
    apply(theme);
    // Button text stays empty; the ::before glyph flips via [data-theme]
    // selectors in style.css. Visible affordance stays language-neutral so a
    // Japanese or Portuguese visitor doesn't see a stray English word.
  }

  // Run immediately so the page never flashes in the wrong theme.
  apply(current());

  // Follow system changes, but only if the user hasn't explicitly picked a theme.
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
      if (localStorage.getItem(KEY) === null) apply(e.matches ? 'dark' : 'light');
    });
  }

  // Wire up the toggle button once DOM is parsed.
  function wire() {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const next = current() === 'dark' ? 'light' : 'dark';
      setAndPersist(next);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
