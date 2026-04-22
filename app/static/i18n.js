// Language picker + locale persistence. Runs once on load.
//
// On first visit the server's Accept-Language / default-en resolution
// picks the locale; from then on this script honors whatever the user
// chose in the picker via:
//
//   * localStorage["ephemera_lang_v1"] (survives across tabs)
//   * ephemera_lang_v1=<tag>; cookie    (read by the server on next nav)
//   * PATCH /api/me/language             (persisted to users.preferred_language
//                                         when authed; no-op 204 otherwise)
//
// The ContextVar + middleware resolve the picker's cookie on the next
// request, so a page reload after setLocale() renders every server-side
// {{ _("...") }} in the new locale. Translation of JS-injected strings
// lands in a follow-up commit once the i18next shim is wired in.
(() => {
  const KEY = 'ephemera_lang_v1';
  const COOKIE_MAX_AGE = 60 * 60 * 24 * 365;  // one year

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
    // Persist client-side first so the fallback path (network blip,
    // server 5xx) still remembers the choice on next nav.
    localStorage.setItem(KEY, lang);
    writeCookie(KEY, lang);
    // PATCH may 401/400 for logged-out or bad input; we don't bail on
    // failure because the cookie is already authoritative for anonymous
    // users and the reload will still pick up the new language.
    try {
      await fetch('/api/me/language', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ language: lang }),
      });
    } catch (_) {
      // Network error -- still reload; the cookie will drive the next
      // request.
    }
    window.location.reload();
  }

  function wirePicker() {
    const sel = document.getElementById('lang-picker');
    if (!sel) return;
    sel.addEventListener('change', (e) => {
      const lang = e.target.value;
      // Don't reload if the user picks what's already active (could
      // happen on mobile double-taps against the <select>).
      if (lang === document.documentElement.lang) return;
      setLocale(lang);
    });
  }

  // Expose for tests + for task #5's i18next wiring to call into once
  // localized JS strings start flowing. Currently just setLocale()
  // matters; t(key) and currentLocale land in the follow-up.
  window.i18n = {
    setLocale,
    get currentLocale() {
      return localStorage.getItem(KEY) || document.documentElement.lang || 'en';
    },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wirePicker);
  } else {
    wirePicker();
  }
})();
