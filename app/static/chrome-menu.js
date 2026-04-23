// Hamburger drawer for the prototype "?chrome=hamburger" variant.
// Inert when not active: the button is display:none, no handlers fire.
//
// The menu rows are purpose-built for a vertical mobile menu -- not
// reused copies of the desktop pills -- so each row needs its own wiring
// here. Sign-out doesn't use the two-click confirm pattern because
// tapping the hamburger + tapping "sign out" is already a two-step
// interaction; a third confirm would just be friction.
(() => {
  const root = document.documentElement;
  const btn = document.getElementById('chrome-menu-btn');
  if (!btn) return;

  const panel = document.getElementById('chrome-menu-panel');
  const userNameEl = document.getElementById('chrome-menu-user-name');
  const langSelect = document.getElementById('chrome-menu-lang');
  const themeBtn = document.getElementById('chrome-menu-theme');
  const themeLabel = document.getElementById('chrome-menu-theme-label');
  const signoutBtn = document.getElementById('chrome-menu-signout');

  function setOpen(open) {
    if (open) root.dataset.chromeMenuOpen = 'true';
    else delete root.dataset.chromeMenuOpen;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (panel) panel.setAttribute('aria-hidden', open ? 'false' : 'true');
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    setOpen(root.dataset.chromeMenuOpen !== 'true');
  });

  // Outside-click closes. Clicks inside the menu container don't close
  // (row handlers close themselves where it matters, e.g. after navigating).
  document.addEventListener('click', (e) => {
    if (root.dataset.chromeMenuOpen !== 'true') return;
    if (e.target.closest('#chrome-menu')) return;
    setOpen(false);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && root.dataset.chromeMenuOpen === 'true') {
      setOpen(false);
      btn.focus();
    }
  });

  // ---- User name row ----
  // sender.js fetches /api/me and writes the username into #user-name.
  // Mirror that into the menu by observing the source node; one watcher,
  // zero duplicate fetches.
  function syncUserName() {
    const src = document.getElementById('user-name');
    if (!src || !userNameEl) return;
    const txt = (src.textContent || '').trim();
    if (txt && txt !== '…' && txt !== '…') userNameEl.textContent = txt;
  }
  syncUserName();
  const userSrc = document.getElementById('user-name');
  if (userSrc && userNameEl) {
    new MutationObserver(syncUserName).observe(userSrc, {
      childList: true, characterData: true, subtree: true,
    });
  }

  // ---- Language row ----
  // Delegates to window.i18n.setLocale so cookie + DB + reload happen
  // through the one path the desktop picker already uses.
  if (langSelect) {
    langSelect.addEventListener('change', (e) => {
      const lang = e.target.value;
      if (window.i18n && typeof window.i18n.setLocale === 'function') {
        window.i18n.setLocale(lang);
      } else {
        // Fallback: reload with ?lang=; server middleware honors it.
        const u = new URL(location.href);
        u.searchParams.set('lang', lang);
        location.href = u.toString();
      }
    });
  }

  // ---- Theme row ----
  // The desktop theme.js wires #theme-toggle and owns the localStorage
  // + <html data-theme> state. We trigger its click programmatically and
  // mirror the label here, rather than duplicating the theme-flip logic.
  function updateThemeLabel() {
    if (!themeLabel) return;
    const theme = document.documentElement.dataset.theme || 'light';
    const light = themeLabel.dataset.light || 'light';
    const dark = themeLabel.dataset.dark || 'dark';
    themeLabel.textContent = theme === 'dark' ? dark : light;
  }
  updateThemeLabel();
  new MutationObserver(updateThemeLabel).observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme'],
  });
  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      const desktop = document.getElementById('theme-toggle');
      if (desktop) desktop.click();
      // Menu stays open so the user can see the theme flip and pick again.
    });
  }

  // ---- Sign-out row ----
  if (signoutBtn) {
    signoutBtn.addEventListener('click', async () => {
      try {
        await fetch('/send/logout', { method: 'POST' });
      } catch {}
      window.location.reload();
    });
  }
})();
