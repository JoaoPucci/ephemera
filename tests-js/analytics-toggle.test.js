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

// URL-aware fetch stub. analytics-toggle.js bootstraps from /api/me on
// init (so it works on every authed page, not just the sender), AND
// PATCHes /api/me/preferences on commit. Tests need both responses
// distinguished -- the bootstrap response shapes the initial state,
// the PATCH response shapes the post-confirm state. Each call is
// served by URL match; `patchResolver` lets a test inject a custom
// PATCH response (e.g. an out-of-order sequence) instead of the
// default {analytics_opt_in: patchOptIn}.
function stubAnalyticsFetch({
  initialOptIn = false,
  patchOptIn = true,
  patchStatus = 200,
  patchResolver = null,
} = {}) {
  return vi.fn((url, opts) => {
    if (url === '/api/me') {
      return Promise.resolve(
        jsonResponse({ id: 1, username: 'admin', analytics_opt_in: initialOptIn })
      );
    }
    if (url === '/api/me/preferences') {
      if (patchResolver) return patchResolver(opts);
      if (patchStatus !== 200) {
        return Promise.resolve(new Response(null, { status: patchStatus }));
      }
      return Promise.resolve(
        jsonResponse({ id: 1, username: 'admin', analytics_opt_in: patchOptIn })
      );
    }
    return Promise.resolve(new Response(null, { status: 404 }));
  });
}

function patchCalls(fetchMock) {
  return fetchMock.mock.calls.filter(([url]) => url === '/api/me/preferences');
}

describe('analytics-toggle.js — bootstrap + cross-surface state sync', () => {
  it('bootstraps initial state from /api/me without waiting for ephemera:me-loaded', async () => {
    // Regression guard: prior versions only listened for the event
    // dispatched by sender.js. On any other authed surface (or future
    // page that doesn't load sender.js), the toggle would render at
    // template default `aria-checked="false"` even for opted-in users.
    mountBothSurfaces({ analyticsOptIn: false });
    vi.stubGlobal('fetch', stubAnalyticsFetch({ initialOptIn: true }));
    await loadModule('analytics-toggle');
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('true');
    expect(document.getElementById('chrome-menu-analytics').getAttribute('aria-checked')).toBe(
      'true'
    );
  });

  it('still syncs from ephemera:me-loaded for in-page consumers that fire it', async () => {
    mountBothSurfaces();
    vi.stubGlobal('fetch', stubAnalyticsFetch({ initialOptIn: false }));
    await loadModule('analytics-toggle');
    await flushAsync();

    window.dispatchEvent(
      new CustomEvent('ephemera:me-loaded', {
        detail: { id: 1, username: 'admin', analytics_opt_in: true },
      })
    );

    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('true');
  });

  it('syncs both surfaces from ephemera:me-updated (cross-surface PATCH propagation)', async () => {
    mountBothSurfaces();
    vi.stubGlobal('fetch', stubAnalyticsFetch({ initialOptIn: false }));
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
  beforeEach(() => mountBothSurfaces({ analyticsOptIn: false }));

  it('clicking the desktop pill (off) opens the popover and does NOT PATCH', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: false });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    const btn = document.getElementById('analytics-toggle');
    btn.click();
    await flushAsync();

    expect(patchCalls(fetchMock)).toHaveLength(0);
    expect(document.getElementById('analytics-popover').hidden).toBe(false);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(btn.getAttribute('aria-checked')).toBe('false');
  });

  it('clicking the drawer row (off) opens the disclosure and does NOT PATCH', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: false });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    const btn = document.getElementById('chrome-menu-analytics');
    btn.click();
    await flushAsync();

    expect(patchCalls(fetchMock)).toHaveLength(0);
    expect(document.getElementById('chrome-menu-analytics-disclosure').hidden).toBe(false);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(btn.getAttribute('aria-checked')).toBe('false');
  });

  it('Cancel closes the dialog without PATCHing or flipping state', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: false });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    const btn = document.getElementById('analytics-toggle');
    btn.click();
    await flushAsync();

    document.querySelector('.analytics-popover-cancel').click();
    await flushAsync();

    expect(patchCalls(fetchMock)).toHaveLength(0);
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    expect(btn.getAttribute('aria-checked')).toBe('false');
  });

  it('Enable PATCHes, closes the dialog, and flips both surfaces to on', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: false, patchOptIn: true });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.querySelector('.analytics-popover-confirm').click();
    await flushAsync();
    await flushAsync();

    const patches = patchCalls(fetchMock);
    expect(patches).toHaveLength(1);
    expect(patches[0][1].method).toBe('PATCH');
    expect(JSON.parse(patches[0][1].body)).toEqual({ analytics_opt_in: true });

    expect(document.getElementById('analytics-popover').hidden).toBe(true);
    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('true');
    expect(document.getElementById('chrome-menu-analytics').getAttribute('aria-checked')).toBe(
      'true'
    );
  });

  it('Esc dismisses the open dialog without PATCHing', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: false });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await flushAsync();

    expect(patchCalls(fetchMock)).toHaveLength(0);
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
  });

  it('outside-click dismisses the open dialog without PATCHing', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: false });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.body.click();
    await flushAsync();

    expect(patchCalls(fetchMock)).toHaveLength(0);
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
  });

  it('a failed PATCH does not flip aria-checked (no optimistic state)', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: false, patchStatus: 500 });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.querySelector('.analytics-popover-confirm').click();
    await flushAsync();
    await flushAsync();

    expect(patchCalls(fetchMock)).toHaveLength(1);
    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('false');
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
  });
});

describe('analytics-toggle.js — opt-OUT is instant + acknowledged (asymmetric)', () => {
  beforeEach(() => mountBothSurfaces({ analyticsOptIn: true }));

  it('clicking the desktop pill while ON PATCHes immediately and shows the ack', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: true, patchOptIn: false });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    document.getElementById('analytics-toggle').click();
    await flushAsync();
    await flushAsync();

    const patches = patchCalls(fetchMock);
    expect(patches).toHaveLength(1);
    expect(JSON.parse(patches[0][1].body)).toEqual({ analytics_opt_in: false });
    expect(document.getElementById('analytics-popover').hidden).toBe(true);
    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('false');

    const srAck = document.getElementById('analytics-toggle-ack');
    expect(srAck.textContent.length).toBeGreaterThan(0);
    const tip = document.getElementById('analytics-toggle-ack-tip');
    expect(tip.classList.contains('is-visible')).toBe(true);
    expect(tip.textContent.length).toBeGreaterThan(0);
  });

  it('clicking the drawer row while ON PATCHes immediately and swaps label text', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: true, patchOptIn: false });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    const drawerBtn = document.getElementById('chrome-menu-analytics');
    const labelEl = drawerBtn.querySelector('.chrome-menu-row-label');
    drawerBtn.click();
    await flushAsync();
    await flushAsync();

    expect(patchCalls(fetchMock)).toHaveLength(1);
    expect(document.getElementById('chrome-menu-analytics-disclosure').hidden).toBe(true);
    expect(drawerBtn.getAttribute('aria-checked')).toBe('false');

    expect(labelEl.textContent.length).toBeGreaterThan(0);
    const drawerAck = drawerBtn.querySelector('.chrome-menu-row-ack');
    expect(drawerAck.textContent.length).toBeGreaterThan(0);
  });

  it('failed PATCH on opt-OUT does NOT flip state and does NOT write ack', async () => {
    const fetchMock = stubAnalyticsFetch({ initialOptIn: true, patchStatus: 500 });
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

describe('analytics-toggle.js — race-resilience on rapid PATCH', () => {
  beforeEach(() => mountBothSurfaces({ analyticsOptIn: false }));

  it('serializes PATCHes: a click during in-flight queues until the current resolves', async () => {
    // Two clicks rapid-fire: A (opt-IN) starts, B (opt-OUT) clicks
    // before A's response lands. Pre-fix the two PATCHes ran
    // concurrently and a "drop stale response" guard couldn't recover
    // when the newer one failed after the older one succeeded server-
    // side ("lost update"). Post-fix: only one PATCH is ever in
    // flight; B waits for A to resolve, then drains. Assertions:
    //   - At-most-one PATCH on the wire while A is pending.
    //   - After A resolves, B's PATCH fires automatically.
    //   - Final state reflects B (the most-recent user intent).
    let resolveA;
    let resolveB;
    let callIdx = 0;
    const fetchMock = vi.fn((url, opts) => {
      if (url === '/api/me') {
        return Promise.resolve(jsonResponse({ id: 1, analytics_opt_in: false }));
      }
      if (url === '/api/me/preferences') {
        callIdx += 1;
        return new Promise((resolve) => {
          const body = JSON.parse(opts.body);
          const responder = () =>
            resolve(jsonResponse({ id: 1, analytics_opt_in: body.analytics_opt_in }));
          if (callIdx === 1) resolveA = responder;
          else resolveB = responder;
        });
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    // Open dialog, confirm -> PATCH A starts. inFlight=true.
    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.querySelector('.analytics-popover-confirm').click();
    await flushAsync();
    expect(patchCalls(fetchMock)).toHaveLength(1);

    // While A pending: simulate the user already on (event sync), then
    // click again -> opt-OUT path. Should QUEUE, not fire PATCH yet.
    window.dispatchEvent(
      new CustomEvent('ephemera:me-updated', {
        detail: { analytics_opt_in: true },
      })
    );
    document.getElementById('analytics-toggle').click();
    await flushAsync();
    // Critical: at-most-one PATCH on the wire. The queued intent has
    // NOT been sent yet.
    expect(patchCalls(fetchMock)).toHaveLength(1);

    // Land A: PATCH B should now drain automatically.
    resolveA();
    await flushAsync();
    await flushAsync();
    expect(patchCalls(fetchMock)).toHaveLength(2);
    // B's body carries the queued intent (false).
    expect(JSON.parse(patchCalls(fetchMock)[1][1].body)).toEqual({ analytics_opt_in: false });

    // Land B: state lands as the most-recent intent (false).
    resolveB();
    await flushAsync();
    await flushAsync();
    expect(document.getElementById('analytics-toggle').getAttribute('aria-checked')).toBe('false');
  });

  it('a queued intent overwrites any prior queued intent (single-slot queue)', async () => {
    // Three rapid clicks while the first PATCH is in flight: only the
    // most recent intent should fire after the in-flight resolves.
    // The two intermediate clicks coalesce into one tail PATCH.
    let resolveFirst;
    const fetchMock = vi.fn((url, opts) => {
      if (url === '/api/me') {
        return Promise.resolve(jsonResponse({ id: 1, analytics_opt_in: false }));
      }
      if (url === '/api/me/preferences') {
        const body = JSON.parse(opts.body);
        if (resolveFirst) {
          // Subsequent calls (queue drain) resolve immediately.
          return Promise.resolve(jsonResponse({ id: 1, analytics_opt_in: body.analytics_opt_in }));
        }
        return new Promise((resolve) => {
          resolveFirst = () =>
            resolve(jsonResponse({ id: 1, analytics_opt_in: body.analytics_opt_in }));
        });
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('analytics-toggle');
    await flushAsync();

    // First PATCH (opt-IN via dialog).
    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.querySelector('.analytics-popover-confirm').click();
    await flushAsync();

    // Queue intent #1 (opt-OUT).
    window.dispatchEvent(
      new CustomEvent('ephemera:me-updated', {
        detail: { analytics_opt_in: true },
      })
    );
    document.getElementById('analytics-toggle').click();
    await flushAsync();
    // Queue intent #2 (opt-IN again -- overwrites #1 in the slot).
    window.dispatchEvent(
      new CustomEvent('ephemera:me-updated', {
        detail: { analytics_opt_in: false },
      })
    );
    document.getElementById('analytics-toggle').click();
    await flushAsync();
    document.querySelector('.analytics-popover-confirm').click();
    await flushAsync();

    expect(patchCalls(fetchMock)).toHaveLength(1); // only first is in flight

    // Drain. Only one tail PATCH (intent #2) -- intent #1 was overwritten.
    resolveFirst();
    await flushAsync();
    await flushAsync();
    await flushAsync();
    expect(patchCalls(fetchMock)).toHaveLength(2);
    expect(JSON.parse(patchCalls(fetchMock)[1][1].body)).toEqual({ analytics_opt_in: true });
  });
});
