import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse, loadModule } from './helpers.js';

// ---------------------------------------------------------------------------
// status-poll.js fetches /api/secrets/{id}/status every 5 seconds and paints
// a pending/viewed/burned/expired/gone pill. The existing sender.test.js
// suite exercises the happy-path-with-track flow indirectly through the
// full submit pipeline, but it doesn't reach the network-error branches in
// fetchStatus or the viewed_at body of paintStatus. This dedicated suite
// pins those paths.
// ---------------------------------------------------------------------------

function mountStatusFixture() {
  document.body.innerHTML = `
    <span id="status-value" class="status-pill pending">pending</span>
    <span id="status-detail" class="muted-inline"></span>
    <ul id="tracked-list"></ul>
    <section id="tracked-section" hidden>
      <button type="button" id="tracked-header" aria-expanded="false">
        <span class="tracked-panel-title">Tracked</span>
        <span id="tracked-count">0</span>
      </button>
      <div id="tracked-body">
        <ul id="tracked-list"></ul>
        <button type="button" id="tracked-clear" hidden>
          <span id="tracked-clear-label">Clear past entries</span>
        </button>
      </div>
    </section>
  `;
}

function valueEl() {
  return document.getElementById('status-value');
}
function detailEl() {
  return document.getElementById('status-detail');
}

beforeEach(mountStatusFixture);

afterEach(() => {
  vi.useRealTimers();
});

describe('status-poll.js — fetchStatus result paints', () => {
  it('200 with status=pending paints pending and clears detail', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ status: 'pending', viewed_at: null }))
    );
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');

    expect(valueEl().classList.contains('pending')).toBe(true);
    // status.pending in en.json is lowercase "pending".
    expect(valueEl().textContent).toBe('pending');
    expect(detailEl().textContent).toBe('');

    stopStatusPoll();
  });

  it('200 with status=viewed + viewed_at paints viewed AND fills detail with prefix + timestamp', async () => {
    vi.useFakeTimers();
    const viewedAt = '2026-04-29T12:34:56Z';
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ status: 'viewed', viewed_at: viewedAt }))
    );
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');

    expect(valueEl().classList.contains('viewed')).toBe(true);
    expect(valueEl().textContent).toBe('viewed');
    // sender.viewed_at_prefix is "at " in en.json. The exact timestamp
    // formatting is locale-dependent (.toLocaleString varies by host
    // environment), so we assert the prefix landed and the year (most
    // stable signal across locales) appears after it.
    const detail = detailEl().textContent;
    expect(detail).toContain('at ');
    expect(detail).toContain('2026');

    stopStatusPoll();
  });

  it('removes prior status classes before adding the new one', async () => {
    vi.useFakeTimers();
    valueEl().classList.add('pending');
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ status: 'burned', viewed_at: '2026-04-29T12:34:56Z' }))
    );
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');

    expect(valueEl().classList.contains('burned')).toBe(true);
    expect(valueEl().classList.contains('pending')).toBe(false);
  });
});

describe('status-poll.js — fetchStatus failure paths', () => {
  it('404 paints the gone pill (server-side row is gone -- secret destroyed)', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 404 })));
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');

    expect(valueEl().classList.contains('gone')).toBe(true);
    // status.gone in en.json renders as "no longer tracked" -- the
    // user-facing copy frames it as "the row is no longer in your list"
    // rather than the more accurate-but-jarring "gone".
    expect(valueEl().textContent).toBe('no longer tracked');

    stopStatusPoll();
  });

  it('non-404 error response (e.g. 500) is treated as no-data: paints pending and keeps polling', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 500 })));
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');

    // No data -> defaults to 'pending' so the pill stays neutral and the
    // next tick has a chance to recover.
    expect(valueEl().classList.contains('pending')).toBe(true);
    expect(valueEl().textContent).toBe('pending');

    stopStatusPoll();
  });

  it('rate-limit response (429) is treated the same -- defensive null + pending', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 429 })));
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');

    expect(valueEl().classList.contains('pending')).toBe(true);

    stopStatusPoll();
  });

  it('network error (fetch throws) is caught and treated as no-data', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() => Promise.reject(new Error('offline')));
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    // Must not throw -- the catch in fetchStatus swallows.
    await expect(startStatusPoll('s1')).resolves.toBeUndefined();
    expect(valueEl().classList.contains('pending')).toBe(true);

    stopStatusPoll();
  });
});

describe('status-poll.js — terminal status stops polling + refreshes tracked list', () => {
  it('a viewed status stops polling and triggers a tracked-list re-fetch', async () => {
    vi.useFakeTimers();
    const trackedFetch = vi.fn(() => Promise.resolve(jsonResponse({ items: [] })));
    let statusCalls = 0;
    const fetchMock = vi.fn((url) => {
      if (url.includes('/api/secrets/tracked')) return trackedFetch(url);
      if (url.includes('/status')) {
        statusCalls++;
        return Promise.resolve(
          jsonResponse({ status: 'viewed', viewed_at: '2026-04-29T12:00:00Z' })
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');
    // One status call happened on initial tick. Advance the fake clock
    // by 10s -- if polling is still alive, two more status calls would
    // fire. We assert it stopped after the first.
    await vi.advanceTimersByTimeAsync(10_000);

    expect(statusCalls).toBe(1);
    // tracked-list was re-fetched as part of the terminal-status branch.
    const trackedHits = fetchMock.mock.calls.filter(([u]) => u.includes('/api/secrets/tracked'));
    expect(trackedHits.length).toBeGreaterThan(0);

    stopStatusPoll();
  });

  it('a pending status keeps polling -- next tick fires after 5s', async () => {
    vi.useFakeTimers();
    let statusCalls = 0;
    const fetchMock = vi.fn((url) => {
      if (url.includes('/status')) {
        statusCalls++;
        return Promise.resolve(jsonResponse({ status: 'pending', viewed_at: null }));
      }
      return Promise.resolve(jsonResponse({ items: [] }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');
    expect(statusCalls).toBe(1);

    // 5s -> second tick fires
    await vi.advanceTimersByTimeAsync(5000);
    expect(statusCalls).toBe(2);

    // 5s more -> third tick
    await vi.advanceTimersByTimeAsync(5000);
    expect(statusCalls).toBe(3);

    stopStatusPoll();
  });
});

describe('status-poll.js — stopStatusPoll lifecycle', () => {
  it('stopStatusPoll before any startStatusPoll is a no-op (no error)', async () => {
    const { stopStatusPoll } = await loadModule('sender/status-poll');
    expect(() => stopStatusPoll()).not.toThrow();
  });

  it('stopStatusPoll after start clears the interval (no further ticks)', async () => {
    vi.useFakeTimers();
    let statusCalls = 0;
    const fetchMock = vi.fn(() => {
      statusCalls++;
      return Promise.resolve(jsonResponse({ status: 'pending', viewed_at: null }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');
    expect(statusCalls).toBe(1);

    stopStatusPoll();

    // Even past the 5s tick boundary, no more status calls fire.
    await vi.advanceTimersByTimeAsync(20_000);
    expect(statusCalls).toBe(1);
  });

  it('startStatusPoll twice on the same handle stops the prior interval', async () => {
    vi.useFakeTimers();
    let statusCalls = 0;
    const fetchMock = vi.fn(() => {
      statusCalls++;
      return Promise.resolve(jsonResponse({ status: 'pending', viewed_at: null }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { startStatusPoll, stopStatusPoll } = await loadModule('sender/status-poll');

    await startStatusPoll('s1');
    await startStatusPoll('s2');
    // Two initial-tick fetches (one per startStatusPoll call).
    expect(statusCalls).toBe(2);

    // 5s -> ONE additional fetch (the second poll's 5s tick), not two.
    await vi.advanceTimersByTimeAsync(5000);
    expect(statusCalls).toBe(3);

    stopStatusPoll();
  });
});
