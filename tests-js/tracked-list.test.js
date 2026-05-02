import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mountTrackedFixture } from './fixtures/tracked.js';
import { flushAsync, jsonResponse, loadModule } from './helpers.js';

// ---------------------------------------------------------------------------
// Item builders. Defaults match a pending text secret with no label and no
// cached URL (the orphan shape). Tests override per-field.
// ---------------------------------------------------------------------------

function item(overrides = {}) {
  return {
    id: 'id-1',
    content_type: 'text',
    mime_type: null,
    label: null,
    status: 'pending',
    created_at: '2026-04-28T11:00:00Z',
    expires_at: '2026-04-29T11:00:00Z',
    viewed_at: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// fetch stub. Records calls keyed by URL/method so each test can assert on
// what fired without re-grepping mock.calls. Default routes return safe
// shapes (empty list, 204 on mutations); per-test overrides shadow them.
// ---------------------------------------------------------------------------

function makeFetchStub({
  trackedItems = [],
  cancelStatus = 204,
  deleteStatus = 204,
  clearStatus = 200,
  clearedCount = 0,
  trackedStatus = 200,
  trackedThrows = false,
} = {}) {
  const calls = {
    tracked: [],
    cancel: [],
    delete: [],
    clear: [],
  };
  function handleTracked(url, opts) {
    calls.tracked.push({ url, opts });
    if (trackedThrows) return Promise.reject(new Error('network'));
    if (trackedStatus !== 200) {
      return Promise.resolve(new Response(null, { status: trackedStatus }));
    }
    return Promise.resolve(jsonResponse({ items: trackedItems }));
  }

  function handleClear(url, opts) {
    calls.clear.push({ url, opts });
    if (clearStatus !== 200) {
      return Promise.resolve(new Response(null, { status: clearStatus }));
    }
    return Promise.resolve(jsonResponse({ cleared: clearedCount }));
  }

  function handleCancel(match, opts) {
    calls.cancel.push({ id: decodeURIComponent(match[1]), opts });
    return Promise.resolve(new Response(null, { status: cancelStatus }));
  }

  function handleDelete(match, opts) {
    calls.delete.push({ id: decodeURIComponent(match[1]), opts });
    return Promise.resolve(new Response(null, { status: deleteStatus }));
  }

  const fn = vi.fn((url, opts = {}) => {
    if (url === '/api/secrets/tracked') return handleTracked(url, opts);
    if (url === '/api/secrets/tracked/clear' && opts.method === 'POST') {
      return handleClear(url, opts);
    }
    const cancelMatch = url.match(/^\/api\/secrets\/([^/]+)\/cancel$/);
    if (cancelMatch && opts.method === 'POST') return handleCancel(cancelMatch, opts);
    const deleteMatch = url.match(/^\/api\/secrets\/([^/]+)$/);
    if (deleteMatch && opts.method === 'DELETE') return handleDelete(deleteMatch, opts);
    return Promise.resolve(new Response(null, { status: 404 }));
  });
  return { fn, calls };
}

// `gcUrls` walks localStorage; each test starts with a clean slate so cached
// URLs from a prior test can't bleed into the orphan/copyable assertions.
beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

// Pre-seed url-cache by writing the same key url-cache.js writes. Avoids
// having to also load that module in tests where we just want a cached row.
function seedUrl(id, url) {
  const existing = JSON.parse(localStorage.getItem('ephemera_urls_v1') || '{}');
  existing[id] = url;
  localStorage.setItem('ephemera_urls_v1', JSON.stringify(existing));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('tracked-list.js — render: empty + visibility', () => {
  beforeEach(mountTrackedFixture);

  it('hides the section when the server returns an empty list', async () => {
    const { fn } = makeFetchStub({ trackedItems: [] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const section = document.getElementById('tracked-section');
    expect(section.hidden).toBe(true);
    expect(document.getElementById('tracked-list').children.length).toBe(0);
  });

  it('reveals the section and writes the count when items exist', async () => {
    const { fn } = makeFetchStub({
      trackedItems: [
        item({ id: 'a' }),
        item({ id: 'b', status: 'viewed', viewed_at: '2026-04-28T11:30:00Z' }),
      ],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    expect(document.getElementById('tracked-section').hidden).toBe(false);
    expect(document.getElementById('tracked-count').textContent).toBe('2');
    expect(document.getElementById('tracked-list').children.length).toBe(2);
  });

  it('leaves UI alone on a fetch network error (transient errors must not destroy state)', async () => {
    const { fn } = makeFetchStub({ trackedItems: [item()] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList(); // first paint succeeds
    await flushAsync();

    const before = document.getElementById('tracked-list').innerHTML;

    // Re-stub with a throwing fetch and re-render: the existing list must
    // survive untouched so a network blip doesn't blank the user's view.
    const failing = makeFetchStub({ trackedThrows: true });
    vi.stubGlobal('fetch', failing.fn);
    await mod.renderTrackedList();
    await flushAsync();

    expect(document.getElementById('tracked-list').innerHTML).toBe(before);
  });
});

describe('tracked-list.js — fallback labels and tooltips', () => {
  beforeEach(mountTrackedFixture);

  it('falls back to "Text secret" when the row has no label', async () => {
    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a', label: null })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const labelEl = document.querySelector('li[data-id="a"] .label');
    expect(labelEl.textContent).toBe('Text secret');
    // Fallback label should NOT carry a hover tooltip — there's nothing
    // informative to reveal beyond what's already on screen.
    expect(labelEl.title).toBe('');
  });

  it('falls back to "Image secret" for an unlabelled image row', async () => {
    const { fn } = makeFetchStub({
      trackedItems: [item({ id: 'a', content_type: 'image', mime_type: 'image/png', label: null })],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    expect(document.querySelector('li[data-id="a"] .label').textContent).toBe('Image secret');
  });

  it('uses the user-supplied label and adds a hover tooltip with the full text', async () => {
    const { fn } = makeFetchStub({
      trackedItems: [item({ id: 'a', label: 'API key for Acme' })],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const labelEl = document.querySelector('li[data-id="a"] .label');
    expect(labelEl.textContent).toBe('API key for Acme');
    expect(labelEl.title).toBe('API key for Acme');
  });
});

describe('tracked-list.js — copyable vs orphan rows', () => {
  beforeEach(mountTrackedFixture);

  it('marks rows with a cached URL as copyable (role=button, tabindex=0)', async () => {
    seedUrl('a', 'https://host/s/abc#KEY');
    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const li = document.querySelector('li[data-id="a"]');
    expect(li.classList.contains('copyable')).toBe(true);
    expect(li.getAttribute('role')).toBe('button');
    expect(li.getAttribute('tabindex')).toBe('0');
    expect(li.classList.contains('orphan')).toBe(false);
  });

  it('marks rows without a cached URL as orphan and shows the orphan hint', async () => {
    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const li = document.querySelector('li[data-id="a"]');
    expect(li.classList.contains('orphan')).toBe(true);
    expect(li.classList.contains('copyable')).toBe(false);
    expect(li.querySelector('.orphan-hint')).not.toBeNull();
  });

  it('does not mark non-pending rows copyable even when a cached URL is present', async () => {
    // Once viewed/burned/canceled the link is dead; offering "click to copy"
    // would set up the user to share a URL that 404s. Keep the row visible
    // for status, but inert.
    seedUrl('a', 'https://host/s/abc#KEY');
    const { fn } = makeFetchStub({
      trackedItems: [item({ id: 'a', status: 'viewed', viewed_at: '2026-04-28T11:30:00Z' })],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const li = document.querySelector('li[data-id="a"]');
    expect(li.classList.contains('copyable')).toBe(false);
  });
});

describe('tracked-list.js — time text by status', () => {
  beforeEach(mountTrackedFixture);

  // The status branch in renderTrackedList renders a different second-half
  // for each terminal status: "burned <when>" for burned, "canceled <when>"
  // for canceled, "expired <when>" for expired. Each branch lives next to
  // the others; covering one wasn't enough to exercise the branch arm
  // for the others.

  it('appends "burned <relative>" to the time text for a burned row', async () => {
    const { fn } = makeFetchStub({
      trackedItems: [item({ id: 'a', status: 'burned', viewed_at: '2026-04-28T11:30:00Z' })],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const timeEl = document.querySelector('li[data-id="a"] .time');
    expect(timeEl.textContent).toContain('burned');
    // Hover tooltip carries absolute timestamps for both events.
    expect(timeEl.title).toContain('burned');
  });

  it('appends "canceled <relative>" to the time text for a canceled row', async () => {
    const { fn } = makeFetchStub({
      trackedItems: [item({ id: 'a', status: 'canceled', viewed_at: '2026-04-28T11:30:00Z' })],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const timeEl = document.querySelector('li[data-id="a"] .time');
    expect(timeEl.textContent).toContain('canceled');
    expect(timeEl.title).toContain('canceled');
  });

  it('appends "expired <relative>" using expires_at (no viewed_at on expired rows)', async () => {
    // Expired rows differ from viewed/burned/canceled: there's no
    // viewed_at because the secret was never delivered. The branch reads
    // expires_at instead. Hover tooltip's else-branch also reads expires_at
    // (the "viewed_at-not-set, status==expired" arm at line ~215).
    const { fn } = makeFetchStub({
      trackedItems: [
        item({
          id: 'a',
          status: 'expired',
          viewed_at: null,
          expires_at: '2026-04-28T10:00:00Z',
        }),
      ],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const timeEl = document.querySelector('li[data-id="a"] .time');
    expect(timeEl.textContent).toContain('expired');
    expect(timeEl.title).toContain('expired');
  });
});

describe('tracked-list.js — copy-row UX', () => {
  beforeEach(mountTrackedFixture);

  it('clicking a copyable row calls navigator.clipboard.writeText with the cached URL', async () => {
    seedUrl('a', 'https://host/s/abc#KEY');
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const li = document.querySelector('li[data-id="a"]');
    li.click();
    await flushAsync();

    expect(writeText).toHaveBeenCalledWith('https://host/s/abc#KEY');
    expect(li.classList.contains('flash-copy')).toBe(true);
    expect(li.dataset.busy).toBe('1');
  });

  it('Enter on a focused copyable row triggers the copy', async () => {
    // Keyboard activation mirrors the click path so a screen-reader user
    // can copy the URL without a mouse. Without the keydown branch the
    // row's role=button + tabindex=0 would be a lie.
    seedUrl('a', 'https://host/s/abc#KEY');
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const li = document.querySelector('li[data-id="a"]');
    const ev = new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      cancelable: true,
    });
    li.dispatchEvent(ev);
    await flushAsync();

    expect(ev.defaultPrevented).toBe(true);
    expect(writeText).toHaveBeenCalledWith('https://host/s/abc#KEY');
  });

  it('Space on a focused copyable row triggers the copy', async () => {
    seedUrl('a', 'https://host/s/abc#KEY');
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const li = document.querySelector('li[data-id="a"]');
    const ev = new KeyboardEvent('keydown', {
      key: ' ',
      bubbles: true,
      cancelable: true,
    });
    li.dispatchEvent(ev);
    await flushAsync();

    expect(ev.defaultPrevented).toBe(true);
    expect(writeText).toHaveBeenCalledWith('https://host/s/abc#KEY');
  });

  it('other keys (e.g. Tab) do not trigger the copy', async () => {
    seedUrl('a', 'https://host/s/abc#KEY');
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const li = document.querySelector('li[data-id="a"]');
    const ev = new KeyboardEvent('keydown', {
      key: 'Tab',
      bubbles: true,
      cancelable: true,
    });
    li.dispatchEvent(ev);
    await flushAsync();

    expect(ev.defaultPrevented).toBe(false);
    expect(writeText).not.toHaveBeenCalled();
  });

  it('ignores clicks that originated on action buttons inside the row', async () => {
    // Without the .closest('.tracked-cancel, .tracked-remove') guard, hitting
    // the X or the Cancel pill would also trigger the row-level copy.
    seedUrl('a', 'https://host/s/abc#KEY');
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const removeBtn = document.querySelector('li[data-id="a"] .tracked-remove');
    removeBtn.click();
    await flushAsync();

    expect(writeText).not.toHaveBeenCalled();
  });
});

describe('tracked-list.js — per-row cancel (two-click confirm)', () => {
  beforeEach(mountTrackedFixture);

  it('first click arms the cancel button without firing the network call', async () => {
    const { fn, calls } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const cancelBtn = document.querySelector('li[data-id="a"] .tracked-cancel');
    cancelBtn.click();
    await flushAsync();

    expect(cancelBtn.classList.contains('armed')).toBe(true);
    expect(calls.cancel.length).toBe(0);
  });

  it('second click while armed POSTs the cancel and forgets the cached URL', async () => {
    seedUrl('a', 'https://host/s/abc#KEY');
    const { fn, calls } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const cancelBtn = document.querySelector('li[data-id="a"] .tracked-cancel');
    cancelBtn.click(); // arm
    cancelBtn.click(); // execute
    await flushAsync();

    expect(calls.cancel.map((c) => c.id)).toEqual(['a']);
    // forgetUrl wipes localStorage; verify the cache no longer has 'a'.
    const remaining = JSON.parse(localStorage.getItem('ephemera_urls_v1') || '{}');
    expect(remaining.a).toBeUndefined();
  });

  it('auto-disarms the cancel button after 3 seconds without a second click', async () => {
    vi.useFakeTimers();
    const { fn, calls } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await Promise.resolve(); // drain microtasks under fake timers

    const cancelBtn = document.querySelector('li[data-id="a"] .tracked-cancel');
    cancelBtn.click();
    expect(cancelBtn.classList.contains('armed')).toBe(true);

    vi.advanceTimersByTime(3001);
    expect(cancelBtn.classList.contains('armed')).toBe(false);
    expect(calls.cancel.length).toBe(0);
  });

  it('does not render a cancel button on non-pending rows', async () => {
    const { fn } = makeFetchStub({
      trackedItems: [item({ id: 'a', status: 'viewed', viewed_at: '2026-04-28T11:30:00Z' })],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    expect(document.querySelector('li[data-id="a"] .tracked-cancel')).toBeNull();
  });
});

describe('tracked-list.js — per-row remove (X button)', () => {
  beforeEach(mountTrackedFixture);

  it('clicking X DELETEs /api/secrets/{id} and forgets the cached URL', async () => {
    seedUrl('a', 'https://host/s/abc#KEY');
    const { fn, calls } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    document.querySelector('li[data-id="a"] .tracked-remove').click();
    await flushAsync();

    expect(calls.delete.map((c) => c.id)).toEqual(['a']);
    const remaining = JSON.parse(localStorage.getItem('ephemera_urls_v1') || '{}');
    expect(remaining.a).toBeUndefined();
  });
});

describe('tracked-list.js — clear-history (two-click confirm)', () => {
  beforeEach(mountTrackedFixture);

  it('hides the clear button when every row is still pending', async () => {
    const { fn } = makeFetchStub({
      trackedItems: [item({ id: 'a' }), item({ id: 'b' })],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    expect(document.getElementById('tracked-clear').hidden).toBe(true);
  });

  it('shows the clear button when at least one non-pending row exists', async () => {
    const { fn } = makeFetchStub({
      trackedItems: [
        item({ id: 'a' }),
        item({ id: 'b', status: 'viewed', viewed_at: '2026-04-28T11:30:00Z' }),
      ],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    expect(document.getElementById('tracked-clear').hidden).toBe(false);
  });

  it('first click arms the clear button without POSTing', async () => {
    const { fn, calls } = makeFetchStub({
      trackedItems: [item({ id: 'a', status: 'viewed', viewed_at: '2026-04-28T11:30:00Z' })],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const clearBtn = document.getElementById('tracked-clear');
    clearBtn.click();
    await flushAsync();

    expect(clearBtn.classList.contains('armed')).toBe(true);
    expect(calls.clear.length).toBe(0);
  });

  it('second click while armed POSTs /api/secrets/tracked/clear', async () => {
    const { fn, calls } = makeFetchStub({
      trackedItems: [item({ id: 'a', status: 'viewed', viewed_at: '2026-04-28T11:30:00Z' })],
      clearedCount: 1,
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const clearBtn = document.getElementById('tracked-clear');
    clearBtn.click();
    clearBtn.click();
    await flushAsync();

    expect(calls.clear.length).toBe(1);
  });
});

describe('tracked-list.js — panel toggle', () => {
  beforeEach(mountTrackedFixture);

  it('clicking the header flips section.open and aria-expanded', async () => {
    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await flushAsync();

    const section = document.getElementById('tracked-section');
    const header = document.getElementById('tracked-header');
    expect(section.classList.contains('open')).toBe(false);
    expect(header.getAttribute('aria-expanded')).toBe('false');

    header.click();
    expect(section.classList.contains('open')).toBe(true);
    expect(header.getAttribute('aria-expanded')).toBe('true');

    header.click();
    expect(section.classList.contains('open')).toBe(false);
    expect(header.getAttribute('aria-expanded')).toBe('false');
  });
});

describe('tracked-list.js — polling lifecycle', () => {
  beforeEach(mountTrackedFixture);

  it('starts polling when at least one pending row exists', async () => {
    vi.useFakeTimers();
    const setIntervalSpy = vi.spyOn(window, 'setInterval');
    const { fn } = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await Promise.resolve();

    expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 5000);
  });

  it('does NOT start polling when every row is already terminal', async () => {
    vi.useFakeTimers();
    const setIntervalSpy = vi.spyOn(window, 'setInterval');
    const { fn } = makeFetchStub({
      trackedItems: [item({ id: 'a', status: 'viewed', viewed_at: '2026-04-28T11:30:00Z' })],
    });
    vi.stubGlobal('fetch', fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await Promise.resolve();

    expect(setIntervalSpy).not.toHaveBeenCalled();
  });

  it('skips re-rendering when busy=1 is set on a row (in-flight copy flash)', async () => {
    vi.useFakeTimers();
    // First paint with one pending row.
    const stub = makeFetchStub({ trackedItems: [item({ id: 'a' })] });
    vi.stubGlobal('fetch', stub.fn);
    const mod = await loadModule('sender/tracked-list');
    await mod.renderTrackedList();
    await Promise.resolve();

    // Pretend the user just clicked-to-copy: row carries data-busy="1".
    const li = document.querySelector('li[data-id="a"]');
    li.dataset.busy = '1';
    const beforeHTML = document.getElementById('tracked-list').innerHTML;

    // Server's next poll says the row's status changed -- but because
    // the row is busy (mid-animation), the diff path must skip rerender
    // so the flash isn't interrupted.
    const next = makeFetchStub({
      trackedItems: [item({ id: 'a', status: 'viewed', viewed_at: '2026-04-28T11:30:00Z' })],
    });
    vi.stubGlobal('fetch', next.fn);
    await vi.advanceTimersByTimeAsync(5000);

    // Row still has the pre-flash HTML; the new (server-truthful) status
    // will paint on the next poll once busy is cleared.
    expect(document.getElementById('tracked-list').innerHTML).toBe(beforeHTML);
  });
});
