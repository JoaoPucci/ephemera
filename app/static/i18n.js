// Language picker + translation shim.
//
// Two responsibilities:
//   1. Picker: change event on <select id="lang-picker"> -> setLocale(lang)
//      writes localStorage + cookie, fires PATCH /api/me/language (persists
//      for authed users; 204 no-op for anonymous), and reloads so every
//      server-rendered {{ _("...") }} flips to the new locale.
//   2. t(key, vars): dotted-key lookup into the catalog embedded in
//      <script type="application/json" id="i18n-catalog"> by the Jinja
//      layout. Falls through to the English catalog in #i18n-fallback on
//      any miss so a stub locale catalog (just `{}`) still renders
//      English instead of literal key names.
//
// Inline-JSON-not-inline-JS because the CSP is script-src 'self': a
// <script>window.X = ...</script> block would be blocked; a
// <script type="application/json">...</script> block is treated as data
// and is CSP-safe.
(() => {
  const KEY = 'ephemera_lang_v1';
  const COOKIE_MAX_AGE = 60 * 60 * 24 * 365;  // one year

  function parseJsonTag(id) {
    const el = document.getElementById(id);
    if (!el) return {};
    try { return JSON.parse(el.textContent || '{}'); } catch { return {}; }
  }

  const catalog = parseJsonTag('i18n-catalog');
  const fallback = parseJsonTag('i18n-fallback');
  const activeLocale = document.documentElement.lang || 'en';

  function lookup(tree, key) {
    // Dotted-key traversal. t("error.network") walks tree.error.network.
    // Returns undefined on any missing segment so the caller can fall
    // through to the next source.
    let cur = tree;
    for (const seg of key.split('.')) {
      if (cur == null || typeof cur !== 'object') return undefined;
      cur = cur[seg];
    }
    return typeof cur === 'string' ? cur : undefined;
  }

  function interpolate(template, vars) {
    // {{name}} -> vars.name. Unknown vars stay as the literal {{name}} so
    // missing-variable bugs are visible in the UI rather than silently
    // producing empty strings.
    if (!vars) return template;
    return template.replace(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g, (m, name) =>
      name in vars ? String(vars[name]) : m,
    );
  }

  function t(key, vars) {
    const hit = lookup(catalog, key) ?? lookup(fallback, key);
    if (hit === undefined) return key;   // visible sentinel for missing keys
    return interpolate(hit, vars);
  }

  function readCookie(name) {
    const prefix = name + '=';
    for (const p of document.cookie.split('; ')) {
      if (p.startsWith(prefix)) return decodeURIComponent(p.slice(prefix.length));
    }
    return null;
  }

  function writeCookie(name, value) {
    document.cookie = `${name}=${encodeURIComponent(value)}; Path=/; Max-Age=${COOKIE_MAX_AGE}; SameSite=Lax`;
  }

  async function setLocale(lang) {
    localStorage.setItem(KEY, lang);
    writeCookie(KEY, lang);
    try {
      await fetch('/api/me/language', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ language: lang }),
      });
    } catch (_) {
      // Cookie is already authoritative; the reload will honour it.
    }
    window.location.reload();
  }

  function wirePicker() {
    const sel = document.getElementById('lang-picker');
    if (!sel) return;
    sel.addEventListener('change', (e) => {
      const lang = e.target.value;
      if (lang === activeLocale) return;
      setLocale(lang);
    });
  }

  window.i18n = {
    t,
    setLocale,
    get currentLocale() { return activeLocale; },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wirePicker);
  } else {
    wirePicker();
  }
})();
