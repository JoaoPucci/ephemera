// Mobile chrome / hamburger drawer fixture for chrome-menu.js tests.
//
// The fixture also includes the desktop "source-of-truth" siblings
// (#user-name, #theme-toggle) that chrome-menu.js mirrors via a
// MutationObserver. Tests that exercise the focus trap call
// makePanelFocusablesVisible() to override `el.offsetParent` (jsdom
// returns null for everything without a layout engine).
export function mountChromeMenu() {
  document.documentElement.removeAttribute('data-theme');
  delete document.documentElement.dataset.chromeMenuOpen;
  document.body.innerHTML = `
    <div id="chrome-menu">
      <button id="chrome-menu-btn"
              aria-expanded="false"
              aria-label="open menu"
              data-label-closed="open menu"
              data-label-open="close menu"></button>
      <div id="chrome-menu-scrim" aria-hidden="true"></div>
      <div id="chrome-menu-panel" aria-hidden="true">
        <span id="chrome-menu-user-name">…</span>
        <select id="chrome-menu-lang">
          <option value="en">English</option>
          <option value="ja" selected>日本語</option>
        </select>
        <span id="chrome-menu-lang-label">日本語</span>
        <button id="chrome-menu-theme" aria-checked="false"></button>
        <button id="chrome-menu-signout" data-label-default="sign out">
          <span id="chrome-menu-signout-label">sign out</span>
        </button>
      </div>
    </div>
    <span id="user-name">admin</span>
    <button id="theme-toggle"></button>
  `;
}

// jsdom doesn't compute layout, so every element has offsetParent === null
// by default. chrome-menu.js's focusableInPanel() filters on
// `el.offsetParent !== null`, which would drop every panel button under
// jsdom and make the focus trap untestable. Tests that exercise the
// trap call this helper to patch offsetParent on the panel's focusable
// elements. Real browsers compute this from layout; we're standing in.
export function makePanelFocusablesVisible() {
  for (const el of document.querySelectorAll(
    '#chrome-menu-panel button, #chrome-menu-panel select'
  )) {
    Object.defineProperty(el, 'offsetParent', {
      configurable: true,
      get: () => document.body,
    });
  }
}
