import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushAsync, jsonResponse, loadModule } from './helpers.js';

// Mount both surfaces (desktop pill + drawer row + their respective
// confirm panels) so the module can wire each. The asymmetric flow is
// the load-bearing UX here: opt-IN must NOT flip until Enable; opt-OUT
// must flip instantly + show an ack. Each test asserts on one side of
// that contract.
function mountBothSurfaces({ analyticsOptIn = false } = {}) {
  const checkedAttr = analyticsOptIn ? 'true' : 'false';
  const expanded = 'false';
  document.body.innerHTML = `
    <button id="analytics-toggle" class="analytics-toggle"
            role="button" aria-checked="${checkedAttr}"
            aria-haspopup="dialog" aria-expanded="${expanded}"
            aria-controls="analytics-popover">
      <span class="analytics-toggle-label" data-i18n="analytics.label"></span>
      <span class="analytics-toggle-dot"></span>
    </button>
    <div id="analytics-popover" role="dialog" hidden>
      <h2 data-i18n="analytics.dialog_title"></h2>
      <p data-i18n="analytics.dialog_body"></p>
      <p data-i18n="analytics.dialog_note"></p>
      <button class="analytics-popover-cancel" data-i18n="analytics.cancel"></button>
      <button class="analytics-popover-confirm" data-i18n="analytics.confirm"></button>
    </div>
    <span id="analytics-toggle-ack" class="visually-hidden"
          data-i18n-disabled-ack="analytics.disabled_ack"></span>
    <span id="analytics-toggle-ack-tip" class="analytics-toggle-ack-tip"
          data-i18n-disabled-ack="analytics.disabled_ack"></span>

    <button id="chrome-menu-analytics" class="chrome-menu-row chrome-menu-row-toggle"
            role="button" aria-checked="${checkedAttr}"
            aria-haspopup="true" aria-expanded="${expanded}"
            aria-controls="chrome-menu-analytics-disclosure">
      <span class="chrome-menu-row-label" data-i18n="analytics.dialog_title"></span>
      <span class="chrome-menu-row-ack" data-i18n-disabled-ack="analytics.disabled_ack"></span>
    </button>
    <div id="chrome-menu-analytics-disclosure" hidden>
      <p data-i18n="analytics.dialog_body"></p>
      <p data-i18n="analytics.dialog_note"></p>
      <button class="chrome-menu-row-disclosure-cancel" data-i18n="analytics.cancel"></button>
      <button class="chrome-menu-row-disclosure-confirm" data-i18n="analytics.confirm"></button>
    </div>
  `;
}

const PATCH_OK_BODY = { id: 1, username: 'admin', analytics_opt_in: true };
const PATCH_OK_OFF = { id: 1, username: 'admin', analytics_opt_in: false };

describe('analytics-toggle.js — initial state sync', () => {
  beforeEach(() => mountBothSurfaces());

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

describe('analytics-toggle.js — opt-IN goes through the confirm dialog', () => {
  it('clicking the desktop pill (off) opens the popover and does NOT PATCH', async () => {
    mountBothSurfaces({ analyticsOptIn: false });
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(PATCH_OK_BODY)));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    const btn = document.getElementById('analytics-toggle');
    btn.click();
    await flushAsync();

    // No PATCH yet -- the click only opens the dialog.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(document.getElementById('analytics-popover').hidden).toBe(false);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(btn.getAttribute('aria-checked')).toBe('false');
  });

  it('clicking the drawer row (off) opens the disclosure and does NOT PATCH', async () => {
    mountBothSurfaces({ analyticsOptIn: false });
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(PATCH_OK_BODY)));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    const btn = document.getElementById('chrome-menu-analytics');
    btn.click();
    await flushAsync();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(document.getElementById('chrome-menu-analytics-disclosure').hidden).toBe(false);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(btn.getAttribute('aria-checked')).toBe('false');
  });

  it('Cancel closes the dialog without PATCHing or flipping state', async () => {
    mountBothSurfaces({ analyticsOptIn: false });
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(PATCH_OK_BODY)));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    const btn = document.getElementById('analytics-toggle');
    btn.click();
    await flushAsync();

    document.querySelector('.analytics-popover-cancel').click();
    await flushAsync();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    expect(btn.getAttribute('aria-checked')).toBe('false');
  });

  it('Enable PATCHes, closes the dialog, and flips both surfaces to on', async () => {
    mountBothSurfaces({ analyticsOptIn: false });
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(PATCH_OK_BODY)));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.querySelector('.analytics-popover-confirm').click();
    await flushAsync();
    await flushAsync();

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/me/preferences');
    expect(opts.method).toBe('PATCH');
    expect(JSON.parse(opts.body)).toEqual({ analytics_opt_in: true });

    expect(document.getElementById('analytics-popover').hidden).toBe(true);
    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('true');
    expect(document.getElementById('chrome-menu-analytics').getAttribute('aria-checked')).toBe(
      'true'
    );
  });

  it('Esc dismisses the open dialog without PATCHing', async () => {
    mountBothSurfaces({ analyticsOptIn: false });
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(PATCH_OK_BODY)));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await flushAsync();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
  });

  it('outside-click dismisses the open dialog without PATCHing', async () => {
    mountBothSurfaces({ analyticsOptIn: false });
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(PATCH_OK_BODY)));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    // Click on the body (outside both panel and trigger).
    document.body.click();
    await flushAsync();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
  });

  it('a failed PATCH does not flip aria-checked (no optimistic state)', async () => {
    mountBothSurfaces({ analyticsOptIn: false });
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 500 })));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.querySelector('.analytics-popover-confirm').click();
    await flushAsync();
    await flushAsync();

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('false');
    // Dialog still closes -- next click reopens it, which is the user's
    // expected mental model after a failure.
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
  });
});

describe('analytics-toggle.js — opt-OUT is instant + acknowledged (asymmetric)', () => {
  it('clicking the desktop pill while ON PATCHes immediately and shows the ack', async () => {
    mountBothSurfaces({ analyticsOptIn: true });
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(PATCH_OK_OFF)));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    await flushAsync();

    expect(fetchMock).toHaveBeenCalledOnce();
    const [, opts] = fetchMock.mock.calls[0];
    expect(JSON.parse(opts.body)).toEqual({ analytics_opt_in: false });
    // No popover for opt-OUT.
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('false');
    // SR ack: aria-live=polite span carries the spoken confirmation.
    const srAck = document.getElementById('analytics-toggle-ack');
    expect(srAck.textContent.length).toBeGreaterThan(0);
    // Sighted ack: position-fixed tooltip visible briefly. Use the
    // .is-visible class as the visibility signal (CSS opacity-fade);
    // textContent presence sanity-checks the text was set.
    const tip = document.getElementById('analytics-toggle-ack-tip');
    expect(tip.classList.contains('is-visible')).toBe(true);
    expect(tip.textContent.length).toBeGreaterThan(0);
  });

  it('clicking the drawer row while ON PATCHes immediately and swaps label text', async () => {
    mountBothSurfaces({ analyticsOptIn: true });
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(PATCH_OK_OFF)));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    const drawerBtn = document.getElementById('chrome-menu-analytics');
    const labelEl = drawerBtn.querySelector('.chrome-menu-row-label');
    drawerBtn.click();
    await flushAsync();
    await flushAsync();

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(document.getElementById('chrome-menu-analytics-disclosure').hidden).toBe(true);
    expect(drawerBtn.getAttribute('aria-checked')).toBe('false');

    // Sighted: row label briefly carries the ack text (label-swap, same
    // pattern as sign-out two-click). Same row width as the original
    // label so no reflow.
    expect(labelEl.textContent.length).toBeGreaterThan(0);
    // SR: aria-live span has the same text.
    const drawerAck = drawerBtn.querySelector('.chrome-menu-row-ack');
    expect(drawerAck.textContent.length).toBeGreaterThan(0);
  });

  it('failed PATCH on opt-OUT does NOT flip state and does NOT write ack', async () => {
    mountBothSurfaces({ analyticsOptIn: true });
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 500 })));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('true');
    const ack = document.getElementById('analytics-toggle-ack');
    expect(ack.textContent).toBe('');
    const tip = document.getElementById('analytics-toggle-ack-tip');
    expect(tip.classList.contains('is-visible')).toBe(false);
  });
});
