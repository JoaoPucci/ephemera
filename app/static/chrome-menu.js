// Hamburger menu for the mobile chrome. Inert when the variant isn't
// active (no button in the DOM -> bail immediately). Otherwise wires:
//   - open/close + aria-expanded + aria-hidden on panel + scrim
//   - outside-click / scrim-click / Esc to close
//   - basic focus trap while open
//   - language row: delegates to window.i18n.setLocale
//   - theme row: delegates to the desktop toggle, syncs the switch state
//   - sign-out row: two-click confirm via shared helper -- same pattern
//     as the desktop user pill, just a different confirm-key scope
//   - user name: mirrors #user-name so the drawer header populates once
//     sender.js resolves /api/me
//
// Loaded as `<script type="module">` from _layout.html so the import
// below resolves; window.i18n is set by the classic-script i18n.js
// in <head> before this module's deferred load runs.
import { bindTwoClickConfirm } from './two-click.js';

(() => {
  const root = document.documentElement;
  const menu = document.getElementById('chrome-menu');
  const btn = document.getElementById('chrome-menu-btn');
  if (!menu || !btn) return;

  const panel = document.getElementById('chrome-menu-panel');
  const scrim = document.getElementById('chrome-menu-scrim');
  const userNameEl = document.getElementById('chrome-menu-user-name');
  const langSelect = document.getElementById('chrome-menu-lang');
  const langLabel = document.getElementById('chrome-menu-lang-label');
  const themeBtn = document.getElementById('chrome-menu-theme');
  const signoutBtn = document.getElementById('chrome-menu-signout');
  const signoutLabel = document.getElementById('chrome-menu-signout-label');

  // ---- Open / close ----

  function focusableInPanel() {
    if (!panel) return [];
    return Array.from(
      panel.querySelectorAll('button, [href], select, input, [tabindex]:not([tabindex="-1"])')
    ).filter((el) => !el.hasAttribute('disabled') && el.offsetParent !== null);
  }

  // Touch-primary devices: blur the tapped target immediately to clear
  // Android Chromium's native focus halo (doesn't fully honor CSS
  // :focus overrides on every version). Use (pointer: coarse) rather
  // than (hover: none) because Samsung devices with an S Pen report
  // hover-capable but still have touch as their primary pointer --
  // (hover: none) misses them and the halo persists after finger taps.
  const isTouchPrimary =
    typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches;

  // aria-label swaps to match the action a tap would take (SR users
  // hear "close menu" while the drawer is open, "open menu" when
  // closed). Template stashes both strings in data attributes so the
  // locale is resolved at render time, not JS time.
  function applyOpenAttributes(open) {
    if (open) root.dataset.chromeMenuOpen = 'true';
    else delete root.dataset.chromeMenuOpen;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    const nextLabel = open ? btn.dataset.labelOpen : btn.dataset.labelClosed;
    if (nextLabel) btn.setAttribute('aria-label', nextLabel);
    if (panel) panel.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (scrim) scrim.setAttribute('aria-hidden', open ? 'false' : 'true');
  }

  function setOpen(open) {
    applyOpenAttributes(open);
    if (open && !isTouchPrimary) {
      // Move focus into the panel so screen readers start there and Esc
      // works without the user having to tab in first. Skipped on touch
      // because moving focus to a hidden <select> overlay would paint
      // the same halo on that row, not clear it.
      const first = focusableInPanel()[0];
      if (first) setTimeout(() => first.focus(), 50);
    }
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    setOpen(root.dataset.chromeMenuOpen !== 'true');
    // On touch devices the hamburger keeps focus after the tap, which
    // paints Android Chromium's native focus halo. Blur explicitly so
    // no element holds focus unless the user actually keyboard-tabbed.
    if (isTouchPrimary && typeof btn.blur === 'function') btn.blur();
  });

  if (scrim) {
    scrim.addEventListener('click', () => setOpen(false));
  }

  // Minimal focus trap: Tab out of the last element wraps to the first,
  // and Shift+Tab out of the first wraps to the last.
  function handlePanelTab(e) {
    if (!panel) return;
    const items = focusableInPanel();
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  // Esc closes and returns focus to the trigger.
  document.addEventListener('keydown', (e) => {
    if (root.dataset.chromeMenuOpen !== 'true') return;
    if (e.key === 'Escape') {
      e.preventDefault();
      setOpen(false);
      btn.focus();
      return;
    }
    if (e.key === 'Tab') handlePanelTab(e);
  });

  // ---- User name mirror ----
  function syncUserName() {
    const src = document.getElementById('user-name');
    if (!src || !userNameEl) return;
    const txt = (src.textContent || '').trim();
    if (txt && txt !== '…') userNameEl.textContent = txt;
  }
  syncUserName();
  const userSrc = document.getElementById('user-name');
  if (userSrc && userNameEl) {
    new MutationObserver(syncUserName).observe(userSrc, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  // ---- Language row ----
  if (langSelect) {
    langSelect.addEventListener('change', (e) => {
      const lang = e.target.value;
      if (window.i18n && typeof window.i18n.setLocale === 'function') {
        window.i18n.setLocale(lang);
      } else {
        const u = new URL(location.href);
        u.searchParams.set('lang', lang);
        location.href = u.toString();
      }
    });
    // Keep the row-value label in sync when user flips the select without
    // committing (e.g. arrow-keys through options before picking). The
    // native popup does its own thing, but if a focused-but-unopened
    // <select> receives input, the label should still read right.
    langSelect.addEventListener('input', (e) => {
      if (langLabel) {
        const opt = e.target.options[e.target.selectedIndex];
        if (opt) langLabel.textContent = opt.textContent;
      }
    });
  }

  // ---- Theme row (switch) ----
  function syncThemeState() {
    if (!themeBtn) return;
    const theme = root.dataset.theme || 'light';
    themeBtn.setAttribute('aria-checked', theme === 'dark' ? 'true' : 'false');
    themeBtn.dataset.theme = theme;
  }
  syncThemeState();
  new MutationObserver(syncThemeState).observe(root, {
    attributes: true,
    attributeFilter: ['data-theme'],
  });
  if (themeBtn) {
    themeBtn.addEventListener('click', (e) => {
      e.preventDefault();
      const desktop = document.getElementById('theme-toggle');
      if (desktop) desktop.click();
      if (isTouchPrimary && typeof themeBtn.blur === 'function') themeBtn.blur();
      // syncThemeState fires via the MutationObserver when the data-theme
      // attribute flips, no need to call it manually here.
    });
  }

  // ---- Sign-out row (two-click confirm) ----
  // Drawer-scoped confirm key (menu.sign_out_confirm) is distinct from
  // the desktop pill's button.sign_out_confirm: the drawer row has full
  // width so it can show the longer copy, while the pill is width-
  // constrained to avoid colliding with the language picker. Splitting
  // the keys lets each surface have register-appropriate phrasing.
  bindTwoClickConfirm(signoutBtn, {
    labelEl: signoutLabel,
    confirmKey: 'menu.sign_out_confirm',
    onConfirm: async () => {
      try {
        await fetch('/send/logout', { method: 'POST' });
      } catch {}
      window.location.reload();
    },
  });
})();
