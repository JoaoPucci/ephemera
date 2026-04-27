import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushAsync, jsonResponse, loadModule } from './helpers.js';

// Mount both surfaces (desktop pill + drawer row) so the module wires
// each. Tests assert that both surfaces stay in sync regardless of which
// one was clicked, since `analytics-toggle.js` shares one handler.
function mountBothSurfaces() {
  document.body.innerHTML = `
    <button type="button" id="analytics-toggle" class="analytics-toggle"
            role="switch" aria-checked="false"></button>
    <div id="chrome-menu">
      <button type="button" id="chrome-menu-analytics" role="menuitemcheckbox"
              aria-checked="false"></button>
    </div>
  `;
}

function mountDesktopOnly() {
  document.body.innerHTML = `
    <button type="button" id="analytics-toggle" class="analytics-toggle"
            role="switch" aria-checked="false"></button>
  `;
}

function mountDrawerOnly() {
  document.body.innerHTML = `
    <div id="chrome-menu">
      <button type="button" id="chrome-menu-analytics" role="menuitemcheckbox"
              aria-checked="false"></button>
    </div>
  `;
}

describe('analytics-toggle.js — initial state', () => {
  beforeEach(() => {
    mountBothSurfaces();
  });

  it('syncs both surfaces from ephemera:me-loaded', async () => {
    await loadModule('analytics-toggle');
    await flushAsync();

    window.dispatchEvent(
      new CustomEvent('ephemera:me-loaded', {
        detail: { id: 1, username: 'admin', analytics_opt_in: true },
      })
    );

    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('true');
    expect(document.getElementById('chrome-menu-analytics').getAttribute('aria-checked')).toBe(
      'true'
    );
  });

  it('syncs both surfaces from ephemera:me-updated', async () => {
    await loadModule('analytics-toggle');
    await flushAsync();

    window.dispatchEvent(
      new CustomEvent('ephemera:me-updated', {
        detail: { analytics_opt_in: true },
      })
    );

    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('true');
    expect(document.getElementById('chrome-menu-analytics').getAttribute('aria-checked')).toBe(
      'true'
    );
  });
});

describe('analytics-toggle.js — click PATCHes /api/me/preferences', () => {
  it('PATCHes from a desktop click and syncs both surfaces', async () => {
    mountBothSurfaces();
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ id: 1, username: 'admin', analytics_opt_in: true }))
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    await flushAsync();

    const calls = fetchMock.mock.calls;
    expect(calls.length).toBe(1);
    expect(calls[0][0]).toBe('/api/me/preferences');
    expect(calls[0][1].method).toBe('PATCH');
    expect(JSON.parse(calls[0][1].body)).toEqual({ analytics_opt_in: true });

    // Both surfaces reflect the persisted state.
    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('true');
    expect(document.getElementById('chrome-menu-analytics').getAttribute('aria-checked')).toBe(
      'true'
    );
  });

  it('PATCHes from a drawer click and syncs both surfaces', async () => {
    mountBothSurfaces();
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ id: 1, username: 'admin', analytics_opt_in: true }))
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('chrome-menu-analytics').click();
    await flushAsync();
    await flushAsync();

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('true');
    expect(document.getElementById('chrome-menu-analytics').getAttribute('aria-checked')).toBe(
      'true'
    );
  });

  it('dispatches ephemera:me-updated on PATCH success', async () => {
    mountDesktopOnly();
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ id: 1, username: 'admin', analytics_opt_in: true }))
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    let received = null;
    window.addEventListener(
      'ephemera:me-updated',
      (e) => {
        received = e.detail;
      },
      { once: true }
    );

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    await flushAsync();

    expect(received).not.toBeNull();
    expect(received.analytics_opt_in).toBe(true);
  });

  it('rolls back the optimistic flip when the PATCH fails', async () => {
    mountDesktopOnly();
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 500 })));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    const btn = document.getElementById('analytics-toggle');
    expect(btn.getAttribute('aria-checked')).toBe('false');

    btn.click();
    await flushAsync();
    await flushAsync();

    expect(btn.getAttribute('aria-checked')).toBe('false');
  });
});

describe('analytics-toggle.js — works with only one surface present', () => {
  it('drawer-only mount still wires the click handler', async () => {
    mountDrawerOnly();
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ id: 1, username: 'admin', analytics_opt_in: true }))
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('chrome-menu-analytics').click();
    await flushAsync();
    await flushAsync();

    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('desktop-only mount still wires the click handler', async () => {
    mountDesktopOnly();
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ id: 1, username: 'admin', analytics_opt_in: true }))
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    await flushAsync();

    expect(fetchMock).toHaveBeenCalledOnce();
  });
});
