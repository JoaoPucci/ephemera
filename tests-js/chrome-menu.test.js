import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushAsync, loadModule } from './helpers.js';

// ---------------------------------------------------------------------------
// DOM fixture: the mobile drawer + its desktop "source-of-truth" siblings
// (#user-name, #theme-toggle) that chrome-menu.js mirrors. Templates render
// data-label-{open,closed} on the trigger so the locale resolves at gettext
// time, not in JS; tests stand in fixed strings here.
// ---------------------------------------------------------------------------

function mountChromeMenu() {
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
// jsdom and make the focus trap untestable. Patch offsetParent on the
// panel's focusable elements so the trap sees a non-empty candidate list.
// Real browsers compute this from layout; we're standing in.
function makePanelFocusablesVisible() {
  for (const el of document.querySelectorAll(
    '#chrome-menu-panel button, #chrome-menu-panel select'
  )) {
    Object.defineProperty(el, 'offsetParent', {
      configurable: true,
      get: () => document.body,
    });
  }
}

afterEach(() => {
  vi.useRealTimers();
  // Don't carry data-theme between tests -- the MutationObserver-based
  // sync makes this load-bearing for any test that swaps themes.
  document.documentElement.removeAttribute('data-theme');
  delete document.documentElement.dataset.chromeMenuOpen;
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('chrome-menu.js — fixture absent', () => {
  it('loads cleanly when neither #chrome-menu nor #chrome-menu-btn exists', async () => {
    document.body.innerHTML = '<div id="not-the-menu"></div>';
    // No throw, no DOM mutation, no listener wiring.
    await expect(loadModule('chrome-menu')).resolves.toBeDefined();
  });
});

describe('chrome-menu.js — open / close lifecycle', () => {
  beforeEach(mountChromeMenu);

  it('clicking the hamburger opens the drawer (sets dataset + aria-expanded + aria-label)', async () => {
    await loadModule('chrome-menu');
    const btn = document.getElementById('chrome-menu-btn');

    btn.click();

    expect(document.documentElement.dataset.chromeMenuOpen).toBe('true');
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(btn.getAttribute('aria-label')).toBe('close menu');
    expect(document.getElementById('chrome-menu-panel').getAttribute('aria-hidden')).toBe('false');
    expect(document.getElementById('chrome-menu-scrim').getAttribute('aria-hidden')).toBe('false');
  });

  it('clicking the hamburger again closes the drawer (mirror flip)', async () => {
    await loadModule('chrome-menu');
    const btn = document.getElementById('chrome-menu-btn');

    btn.click(); // open
    btn.click(); // close

    expect(document.documentElement.dataset.chromeMenuOpen).toBeUndefined();
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    expect(btn.getAttribute('aria-label')).toBe('open menu');
    expect(document.getElementById('chrome-menu-panel').getAttribute('aria-hidden')).toBe('true');
  });

  it('clicking the scrim closes an open drawer', async () => {
    await loadModule('chrome-menu');
    document.getElementById('chrome-menu-btn').click();
    expect(document.documentElement.dataset.chromeMenuOpen).toBe('true');

    document.getElementById('chrome-menu-scrim').click();

    expect(document.documentElement.dataset.chromeMenuOpen).toBeUndefined();
  });
});

describe('chrome-menu.js — Esc + focus trap', () => {
  beforeEach(mountChromeMenu);

  it('Esc while open closes the drawer', async () => {
    // We deliberately don't assert focus restoration here. jsdom doesn't
    // reset document-level listeners between tests, so each `loadModule`
    // call leaves a residual keydown listener bound to a now-stale module
    // instance. The first listener to fire clears `data-chrome-menu-open`,
    // and every subsequent listener (including the current test's) bails
    // on the "is the menu open?" guard before reaching its own `btn.focus()`.
    // The desktop user-pill test in sender.test.js already covers the
    // close-then-focus contract for the same two-click pattern.
    await loadModule('chrome-menu');
    const btn = document.getElementById('chrome-menu-btn');
    btn.click();
    expect(document.documentElement.dataset.chromeMenuOpen).toBe('true');

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

    expect(document.documentElement.dataset.chromeMenuOpen).toBeUndefined();
  });

  it('Esc while closed is a no-op (early return — does not steal focus)', async () => {
    await loadModule('chrome-menu');

    // Park focus somewhere unrelated; Esc-while-closed must not touch it.
    const themeToggle = document.getElementById('theme-toggle');
    themeToggle.focus();
    expect(document.activeElement).toBe(themeToggle);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

    expect(document.activeElement).toBe(themeToggle);
  });

  it('Tab from the last focusable wraps to the first (forward focus trap)', async () => {
    await loadModule('chrome-menu');
    makePanelFocusablesVisible();
    document.getElementById('chrome-menu-btn').click();

    // Focus the last focusable, then dispatch Tab. The trap wraps to the
    // first focusable.
    document.getElementById('chrome-menu-signout').focus();
    const focusSpy = vi.spyOn(document.getElementById('chrome-menu-lang'), 'focus');

    const ev = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    document.dispatchEvent(ev);

    expect(ev.defaultPrevented).toBe(true);
    expect(focusSpy).toHaveBeenCalled();
  });

  it('Shift+Tab from the first focusable wraps to the last (reverse focus trap)', async () => {
    await loadModule('chrome-menu');
    makePanelFocusablesVisible();
    document.getElementById('chrome-menu-btn').click();

    document.getElementById('chrome-menu-lang').focus();
    const focusSpy = vi.spyOn(document.getElementById('chrome-menu-signout'), 'focus');

    const ev = new KeyboardEvent('keydown', {
      key: 'Tab',
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(ev);

    expect(ev.defaultPrevented).toBe(true);
    expect(focusSpy).toHaveBeenCalled();
  });
});

describe('chrome-menu.js — user-name mirror', () => {
  beforeEach(mountChromeMenu);

  it('initial sync copies a real #user-name value into the drawer header', async () => {
    document.getElementById('user-name').textContent = 'alice';
    await loadModule('chrome-menu');

    expect(document.getElementById('chrome-menu-user-name').textContent).toBe('alice');
  });

  it('ignores the "…" placeholder so the drawer never shows the loading sentinel', async () => {
    document.getElementById('user-name').textContent = '…';
    await loadModule('chrome-menu');

    // Drawer keeps its own placeholder until a real value arrives.
    expect(document.getElementById('chrome-menu-user-name').textContent).toBe('…');
  });

  it('MutationObserver picks up later updates to #user-name', async () => {
    document.getElementById('user-name').textContent = '…';
    await loadModule('chrome-menu');
    expect(document.getElementById('chrome-menu-user-name').textContent).toBe('…');

    // sender.js writes the username into #user-name once /api/me lands.
    // The drawer mirrors it via MutationObserver.
    document.getElementById('user-name').textContent = 'alice';
    // MutationObserver callbacks fire on a microtask -- await one tick.
    await flushAsync();

    expect(document.getElementById('chrome-menu-user-name').textContent).toBe('alice');
  });
});

describe('chrome-menu.js — language row', () => {
  beforeEach(mountChromeMenu);

  it('change event delegates to window.i18n.setLocale', async () => {
    const setLocale = vi.fn();
    window.i18n = { ...window.i18n, setLocale };
    await loadModule('chrome-menu');
    // loadModule re-installs the i18n stub after we mutated it; reattach.
    window.i18n.setLocale = setLocale;

    const sel = document.getElementById('chrome-menu-lang');
    sel.value = 'en';
    sel.dispatchEvent(new Event('change', { bubbles: true }));

    expect(setLocale).toHaveBeenCalledWith('en');
  });

  it('input event keeps #chrome-menu-lang-label in sync without committing', async () => {
    await loadModule('chrome-menu');

    const sel = document.getElementById('chrome-menu-lang');
    // Simulate keyboard arrow scrolling onto the "English" option without
    // committing a change. The row-value label should still update so the
    // menu reads correctly.
    sel.value = 'en';
    sel.dispatchEvent(new Event('input', { bubbles: true }));

    expect(document.getElementById('chrome-menu-lang-label').textContent).toBe('English');
  });
});

describe('chrome-menu.js — theme row', () => {
  beforeEach(mountChromeMenu);

  it('clicking the drawer theme button delegates to the desktop #theme-toggle', async () => {
    await loadModule('chrome-menu');

    const desktop = document.getElementById('theme-toggle');
    const desktopClick = vi.spyOn(desktop, 'click');

    document.getElementById('chrome-menu-theme').click();

    expect(desktopClick).toHaveBeenCalledOnce();
  });

  it('aria-checked + dataset.theme stay synced with <html data-theme> via MutationObserver', async () => {
    document.documentElement.dataset.theme = 'light';
    await loadModule('chrome-menu');

    const themeBtn = document.getElementById('chrome-menu-theme');
    expect(themeBtn.getAttribute('aria-checked')).toBe('false');
    expect(themeBtn.dataset.theme).toBe('light');

    // Flip <html data-theme> the way theme.js would. The drawer mirrors it
    // through its MutationObserver -- no manual sync needed.
    document.documentElement.dataset.theme = 'dark';
    await flushAsync();

    expect(themeBtn.getAttribute('aria-checked')).toBe('true');
    expect(themeBtn.dataset.theme).toBe('dark');
  });
});

describe('chrome-menu.js — sign-out two-click confirm', () => {
  beforeEach(mountChromeMenu);

  // Hang the /send/logout promise so the handler's window.location.reload()
  // never fires. jsdom's reload is non-configurable and can't be spied on
  // cleanly; the same trick is used in sender.test.js for the desktop pill.
  function stubLogout() {
    return vi.fn((url) => {
      if (url === '/send/logout') return new Promise(() => {});
      return Promise.resolve(new Response(null, { status: 404 }));
    });
  }

  function signoutBtn() {
    return document.getElementById('chrome-menu-signout');
  }
  function signoutLabel() {
    return document.getElementById('chrome-menu-signout-label').textContent;
  }

  it('first click arms the button without POSTing /send/logout', async () => {
    const fetchMock = stubLogout();
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('chrome-menu');

    signoutBtn().click();
    await flushAsync();

    expect(signoutBtn().classList.contains('armed')).toBe(true);
    // i18n stub returns the dotted key on miss; the catalog has
    // menu.sign_out_confirm so the label flips to its English text.
    expect(signoutLabel()).not.toBe('sign out');
    const logoutCalls = fetchMock.mock.calls.filter(([u]) => u === '/send/logout');
    expect(logoutCalls.length).toBe(0);
  });

  it('second click while armed POSTs /send/logout', async () => {
    const fetchMock = stubLogout();
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('chrome-menu');

    signoutBtn().click(); // arm
    await flushAsync();
    signoutBtn().click(); // confirm
    await flushAsync();

    const logoutCalls = fetchMock.mock.calls.filter(([u]) => u === '/send/logout');
    expect(logoutCalls.length).toBe(1);
    expect(logoutCalls[0][1]?.method).toBe('POST');
  });

  it('auto-disarms after 3 seconds and restores the default label', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', stubLogout());
    await loadModule('chrome-menu');

    signoutBtn().click();
    expect(signoutBtn().classList.contains('armed')).toBe(true);

    vi.advanceTimersByTime(3001);

    expect(signoutBtn().classList.contains('armed')).toBe(false);
    expect(signoutLabel()).toBe('sign out');
  });
});
