import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushAsync, jsonResponse, loadModule } from './helpers.js';

// Minimal drawer fixture. chrome-menu.js bails immediately if either
// `#chrome-menu` or `#chrome-menu-btn` is missing, so all tests must mount
// the wrapper. Only the rows under test get full markup; other rows
// (language, theme, sign-out) are present but minimal so the module's
// initial-state wiring doesn't NPE looking for them.
function mountDrawer() {
  document.body.innerHTML = `
    <div id="chrome-menu">
      <button id="chrome-menu-btn" aria-expanded="false"
              data-label-open="close menu" data-label-closed="open menu"></button>
      <div id="chrome-menu-panel" aria-hidden="true">
        <span id="chrome-menu-user-name"></span>
        <select id="chrome-menu-lang"><option value="en">English</option></select>
        <span id="chrome-menu-lang-label">English</span>
        <button id="chrome-menu-theme" role="menuitemcheckbox" aria-checked="false"></button>
        <button id="chrome-menu-analytics" role="menuitemcheckbox" aria-checked="false"
                aria-describedby="chrome-menu-analytics-help"></button>
        <button id="chrome-menu-signout" data-label-default="sign out">
          <span id="chrome-menu-signout-label">sign out</span>
        </button>
      </div>
      <div id="chrome-menu-scrim"></div>
    </div>
    <span id="user-name">…</span>
  `;
}

describe('chrome-menu.js — analytics opt-in toggle', () => {
  beforeEach(() => {
    mountDrawer();
  });

  it('syncs aria-checked from the ephemera:me-loaded event', async () => {
    await loadModule('chrome-menu');
    await flushAsync();

    const btn = document.getElementById('chrome-menu-analytics');
    expect(btn.getAttribute('aria-checked')).toBe('false');

    window.dispatchEvent(
      new CustomEvent('ephemera:me-loaded', {
        detail: { id: 1, username: 'admin', analytics_opt_in: true },
      })
    );
    expect(btn.getAttribute('aria-checked')).toBe('true');
  });

  it('PATCHes /api/me/preferences with the flipped value on click', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ id: 1, username: 'admin', analytics_opt_in: true }))
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('chrome-menu');
    await flushAsync();

    const btn = document.getElementById('chrome-menu-analytics');
    btn.click();
    await flushAsync();
    await flushAsync();

    const calls = fetchMock.mock.calls;
    expect(calls.length).toBe(1);
    expect(calls[0][0]).toBe('/api/me/preferences');
    expect(calls[0][1].method).toBe('PATCH');
    expect(JSON.parse(calls[0][1].body)).toEqual({ analytics_opt_in: true });
    expect(btn.getAttribute('aria-checked')).toBe('true');
  });

  it('dispatches ephemera:me-updated with the persisted state on PATCH success', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ id: 1, username: 'admin', analytics_opt_in: true }))
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('chrome-menu');
    await flushAsync();

    let received = null;
    window.addEventListener(
      'ephemera:me-updated',
      (e) => {
        received = e.detail;
      },
      { once: true }
    );

    document.getElementById('chrome-menu-analytics').click();
    await flushAsync();
    await flushAsync();

    expect(received).not.toBeNull();
    expect(received.analytics_opt_in).toBe(true);
  });

  it('rolls back the optimistic flip when the PATCH fails', async () => {
    // Optimistic UX: switch animates immediately on click. If the patch
    // 500s or 401s, the switch must snap back so the user-perceived
    // state matches the server.
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 500 })));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('chrome-menu');
    await flushAsync();

    const btn = document.getElementById('chrome-menu-analytics');
    expect(btn.getAttribute('aria-checked')).toBe('false');

    btn.click();
    await flushAsync();
    await flushAsync();

    expect(btn.getAttribute('aria-checked')).toBe('false');
  });
});
