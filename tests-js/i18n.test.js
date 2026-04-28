import { afterEach, describe, expect, it, vi } from 'vitest';
import { mountI18n } from './fixtures/i18n.js';
import { flushAsync, jsonResponse, loadModule } from './helpers.js';

// jsdom's `window.location.reload` is non-configurable, so individual
// properties can't be spied on. Replacing the whole `window.location` via
// Object.defineProperty IS allowed (configurable on the window descriptor),
// so we swap a stub in for each test that exercises setLocale. afterEach
// puts back something reasonable so subsequent tests start clean.
let savedLocation;
function stubLocation() {
  if (savedLocation === undefined) savedLocation = window.location;
  const stub = {
    reload: vi.fn(),
    href: 'http://localhost/send',
    search: '',
    pathname: '/send',
    origin: 'http://localhost',
  };
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: stub,
  });
  return stub;
}

afterEach(() => {
  if (savedLocation !== undefined) {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: savedLocation,
    });
  }
  // Cookie persistence between tests would let one test's setLocale leak
  // into the next. Wipe the locale cookie explicitly. document.cookie
  // assignment is the right tool here -- the real shim uses it too (see
  // i18n.js writeCookie), and Cookie Store API has incomplete Safari
  // support that's not worth importing into a test helper.
  // biome-ignore lint/suspicious/noDocumentCookie: see comment above.
  document.cookie = 'ephemera_lang_v1=; Path=/; Max-Age=0';
  localStorage.clear();
});

// ---------------------------------------------------------------------------
// t() — dotted-key lookup, interpolation, fallback chain
// ---------------------------------------------------------------------------

describe('i18n.js — t() lookup and fallback', () => {
  it('returns the active-catalog string for a top-level key', async () => {
    mountI18n({ catalog: { hello: 'こんにちは' } });
    await loadModule('i18n');
    expect(window.i18n.t('hello')).toBe('こんにちは');
  });

  it('walks dotted keys through nested objects', async () => {
    mountI18n({ catalog: { error: { network: 'ネットワークエラー' } } });
    await loadModule('i18n');
    expect(window.i18n.t('error.network')).toBe('ネットワークエラー');
  });

  it('falls back to the English catalog when the active catalog lacks the key', async () => {
    mountI18n({
      catalog: { only_in_active: 'JA' },
      fallback: { only_in_fallback: 'EN value' },
    });
    await loadModule('i18n');
    expect(window.i18n.t('only_in_fallback')).toBe('EN value');
  });

  it('returns the literal key as a visible sentinel when no source has it', async () => {
    // Sentinel behaviour is load-bearing -- a translator who adds a t() call
    // but forgets the key gets the dotted key rendered in the UI, which is
    // ugly enough to fail review and visible enough to fix in seconds.
    mountI18n({ catalog: {}, fallback: {} });
    await loadModule('i18n');
    expect(window.i18n.t('nope.missing')).toBe('nope.missing');
  });

  it('returns the key when the lookup hits a non-string node (object instead of leaf)', async () => {
    // Half-defined catalog: `error` is an object but `error.network` is a
    // sub-object rather than the leaf string. Treat as miss and fall through.
    mountI18n({ catalog: { error: { network: { wrong_shape: 'x' } } } });
    await loadModule('i18n');
    expect(window.i18n.t('error.network')).toBe('error.network');
  });

  it('survives an empty active catalog by reading entirely from fallback', async () => {
    mountI18n({ catalog: {}, fallback: { greeting: 'hi' } });
    await loadModule('i18n');
    expect(window.i18n.t('greeting')).toBe('hi');
  });

  it('treats a malformed JSON catalog tag as empty (silent fallback)', async () => {
    document.documentElement.setAttribute('lang', 'ja');
    document.body.outerHTML = `
      <body>
        <script type="application/json" id="i18n-catalog">{not valid json</script>
        <script type="application/json" id="i18n-fallback">${JSON.stringify({ k: 'EN' })}</script>
      </body>
    `;
    await loadModule('i18n');
    // Active catalog is empty -> fallback wins.
    expect(window.i18n.t('k')).toBe('EN');
  });
});

describe('i18n.js — t() interpolation', () => {
  it('substitutes {{name}} with vars[name]', async () => {
    mountI18n({ catalog: { greet: 'Hello, {{name}}!' } });
    await loadModule('i18n');
    expect(window.i18n.t('greet', { name: 'Alice' })).toBe('Hello, Alice!');
  });

  it('keeps unknown vars as literal {{name}} so missing-variable bugs are visible', async () => {
    // Surfacing missing vars as {{name}} in the rendered UI is the design
    // choice -- silently rendering empty strings would make typos invisible.
    mountI18n({ catalog: { greet: 'Hello, {{name}}!' } });
    await loadModule('i18n');
    expect(window.i18n.t('greet', { who: 'Alice' })).toBe('Hello, {{name}}!');
  });

  it('coerces non-string vars to strings (numbers in counts are common)', async () => {
    mountI18n({ catalog: { count: 'You have {{n}} items' } });
    await loadModule('i18n');
    expect(window.i18n.t('count', { n: 7 })).toBe('You have 7 items');
  });

  it('returns the template unchanged when no vars argument is passed', async () => {
    mountI18n({ catalog: { tpl: 'static {{name}}' } });
    await loadModule('i18n');
    expect(window.i18n.t('tpl')).toBe('static {{name}}');
  });
});

describe('i18n.js — currentLocale getter', () => {
  it('reads the active locale from <html lang>', async () => {
    mountI18n({ activeLocale: 'pt-BR' });
    await loadModule('i18n');
    expect(window.i18n.currentLocale).toBe('pt-BR');
  });

  it('defaults to "en" when <html lang> is missing', async () => {
    document.documentElement.removeAttribute('lang');
    document.body.outerHTML = `
      <body>
        <script type="application/json" id="i18n-catalog">{}</script>
        <script type="application/json" id="i18n-fallback">{}</script>
      </body>
    `;
    await loadModule('i18n');
    expect(window.i18n.currentLocale).toBe('en');
  });
});

// ---------------------------------------------------------------------------
// Picker wiring + setLocale side effects
// ---------------------------------------------------------------------------

describe('i18n.js — picker wiring', () => {
  it('change event to a new locale fires setLocale (cookie + localStorage)', async () => {
    mountI18n({ activeLocale: 'ja' });
    stubLocation();
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({})))
    );
    await loadModule('i18n');

    const sel = document.getElementById('lang-picker');
    sel.value = 'es';
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    await flushAsync();

    expect(localStorage.getItem('ephemera_lang_v1')).toBe('es');
    expect(document.cookie).toContain('ephemera_lang_v1=es');
  });

  it('change to the SAME locale is a no-op (no cookie write, no reload)', async () => {
    mountI18n({ activeLocale: 'ja' });
    const loc = stubLocation();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({})));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('i18n');

    const sel = document.getElementById('lang-picker');
    sel.value = 'ja'; // same as active
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    await flushAsync();

    expect(loc.reload).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    // Cookie/localStorage must also stay untouched.
    expect(localStorage.getItem('ephemera_lang_v1')).toBeNull();
  });

  it('bails silently when #lang-picker is absent', async () => {
    mountI18n({ hasPicker: false });
    // No throw on import; window.i18n still exposed.
    await expect(loadModule('i18n')).resolves.toBeDefined();
    expect(typeof window.i18n.setLocale).toBe('function');
  });
});

describe('i18n.js — setLocale on anonymous user', () => {
  it('skips the PATCH when body has no data-authenticated', async () => {
    mountI18n({ authenticated: false });
    const loc = stubLocation();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({})));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('i18n');

    // Fire-and-forget. The sync side effects (localStorage + cookie + reload)
    // land before any await -- the reload-stub records the call synchronously.
    window.i18n.setLocale('es');
    await flushAsync();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(localStorage.getItem('ephemera_lang_v1')).toBe('es');
    expect(document.cookie).toContain('ephemera_lang_v1=es');
    expect(loc.reload).toHaveBeenCalledOnce();
  });
});

describe('i18n.js — setLocale on authenticated user', () => {
  it('PATCHes /api/me/language with the chosen tag', async () => {
    mountI18n({ authenticated: true });
    stubLocation();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({})));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('i18n');

    window.i18n.setLocale('pt-BR');
    await flushAsync();

    const patches = fetchMock.mock.calls.filter(([u]) => u === '/api/me/language');
    expect(patches.length).toBe(1);
    expect(patches[0][1]?.method).toBe('PATCH');
    expect(patches[0][1]?.headers?.['Content-Type']).toBe('application/json');
    expect(JSON.parse(patches[0][1]?.body)).toEqual({ language: 'pt-BR' });
  });

  it('still reloads the page when the PATCH fails (cookie wins)', async () => {
    // Network blip on the PATCH must NOT block the locale flip -- the cookie
    // is authoritative for this tab and the reload picks it up. Server's DB
    // copy stays stale until the next picker change, which is fine.
    mountI18n({ authenticated: true });
    const loc = stubLocation();
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network')))
    );
    await loadModule('i18n');

    window.i18n.setLocale('es');
    await flushAsync();
    await flushAsync();

    expect(loc.reload).toHaveBeenCalledOnce();
    expect(localStorage.getItem('ephemera_lang_v1')).toBe('es');
  });

  it('reloads after a successful PATCH', async () => {
    mountI18n({ authenticated: true });
    const loc = stubLocation();
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({})))
    );
    await loadModule('i18n');

    window.i18n.setLocale('es');
    await flushAsync();
    await flushAsync();

    expect(loc.reload).toHaveBeenCalledOnce();
  });
});
