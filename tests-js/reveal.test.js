import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushAsync, jsonResponse, loadModule } from './helpers.js';

// reveal.js reads window.location.pathname and window.location.hash on every
// call. jsdom's `window.location` is not directly assignable, so we override
// the property with our own plain object before loading the script.
function stubLocation({ pathname = '/s/test-token', hash = '#test-key' } = {}) {
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { pathname, hash, reload() {} },
  });
}

function mountLanding() {
  document.body.innerHTML = `
    <main class="card" id="main-card">
      <section id="state-loading"><p>Loading…</p></section>
      <section id="state-ready" hidden>
        <div id="passphrase-wrap" hidden>
          <label for="passphrase">Passphrase</label>
          <div class="input-with-action">
            <input type="password" id="passphrase"
                   maxlength="200"
                   aria-describedby="passphrase-hint"
                   autocomplete="off">
            <button type="button" id="toggle-passphrase" class="input-action"
                    aria-label="show passphrase" aria-pressed="false">show</button>
          </div>
          <p class="form-hint" id="passphrase-hint" hidden aria-live="polite" aria-atomic="true"></p>
        </div>
        <button id="reveal-btn" type="button">Reveal Secret</button>
        <p class="error" id="reveal-error" hidden></p>
      </section>
      <section id="state-text" hidden>
        <pre id="revealed-text"></pre>
        <button type="button" id="copy-btn" class="copy-btn" hidden>Copy to clipboard</button>
      </section>
      <section id="state-image" hidden>
        <img id="revealed-image" alt="" tabindex="0">
      </section>
      <section id="state-gone" hidden><h1>Gone.</h1></section>
    </main>
    <div id="zoom-overlay" hidden>
      <img id="zoom-image" alt="">
      <button type="button" id="zoom-close">close</button>
    </div>
  `;
}

function revealBtn() {
  return document.getElementById('reveal-btn');
}

describe('reveal.js — in-flight guard on the reveal button', () => {
  beforeEach(() => {
    stubLocation();
    mountLanding();
  });

  it('fires exactly one /reveal fetch even when clicked twice rapidly', async () => {
    // /meta returns "ready, no passphrase", /reveal never resolves.
    let metaResolved = false;
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/meta')) {
        metaResolved = true;
        return Promise.resolve(jsonResponse({ passphrase_required: false }));
      }
      return new Promise(() => {});
    });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('reveal');

    // Wait for init() to finish /meta call and show state-ready
    await flushAsync();
    await flushAsync();
    expect(metaResolved).toBe(true);

    revealBtn().click();
    revealBtn().click();
    await flushAsync();

    const revealCalls = fetchMock.mock.calls.filter(([url]) => url.endsWith('/reveal'));
    expect(revealCalls.length).toBe(1);
  });

  it('swaps the button label to "Revealing…" while the request is in flight', async () => {
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/meta')) {
        return Promise.resolve(jsonResponse({ passphrase_required: false }));
      }
      return new Promise(() => {});
    });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('reveal');

    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();

    expect(revealBtn().disabled).toBe(true);
    expect(revealBtn().textContent).toBe('Revealing…');
  });

  it('restores the button label on a wrong-passphrase 401', async () => {
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/meta')) {
        return Promise.resolve(jsonResponse({ passphrase_required: true }));
      }
      return Promise.resolve(jsonResponse({ detail: 'wrong' }, 401));
    });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('reveal');

    await flushAsync();
    await flushAsync();

    document.getElementById('passphrase').value = 'bad';
    revealBtn().click();
    await flushAsync();
    await flushAsync();

    expect(revealBtn().disabled).toBe(false);
    expect(revealBtn().textContent).toBe('Reveal Secret');
    expect(document.getElementById('reveal-error').hidden).toBe(false);
  });

  it('shows the "gone" state on a 410 response', async () => {
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/meta')) {
        return Promise.resolve(jsonResponse({ passphrase_required: false }));
      }
      return Promise.resolve(new Response(null, { status: 410 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('reveal');

    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('state-gone').hidden).toBe(false);
    expect(document.getElementById('state-ready').hidden).toBe(true);
  });

  it('refuses to start without a URL fragment', async () => {
    stubLocation({ pathname: '/s/test-token', hash: '' });
    mountLanding();
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/meta')) {
        return Promise.resolve(jsonResponse({ passphrase_required: false }));
      }
      return new Promise(() => {});
    });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('reveal');

    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();

    const revealCalls = fetchMock.mock.calls.filter(([url]) => url.endsWith('/reveal'));
    expect(revealCalls.length).toBe(0);
    expect(document.getElementById('reveal-error').hidden).toBe(false);
    // Button is restored so the user isn't stuck if they reload with a correct URL.
    expect(revealBtn().disabled).toBe(false);
  });
});

describe('reveal.js — passphrase visibility toggle', () => {
  beforeEach(() => {
    stubLocation();
    mountLanding();
  });

  it('starts masked; clicking the toggle swaps between password and text', async () => {
    // Never-resolving /meta so the toggle is the only code path being exercised.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {}))
    );
    await loadModule('reveal');

    const input = document.getElementById('passphrase');
    const toggle = document.getElementById('toggle-passphrase');

    expect(input.getAttribute('type')).toBe('password');
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.textContent).toBe('show');
    // aria-label stays at the static (template-rendered) value across clicks
    // -- aria-pressed carries the state per the ARIA Authoring Practices
    // toggle pattern. The fixture renders the initial English label.
    expect(toggle.getAttribute('aria-label')).toBe('show passphrase');

    toggle.click();
    expect(input.getAttribute('type')).toBe('text');
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(toggle.textContent).toBe('hide');
    expect(toggle.getAttribute('aria-label')).toBe('show passphrase');

    toggle.click();
    expect(input.getAttribute('type')).toBe('password');
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.textContent).toBe('show');
    expect(toggle.getAttribute('aria-label')).toBe('show passphrase');
  });
});

describe('reveal.js — receiver passphrase approaching-max hint', () => {
  beforeEach(() => {
    stubLocation();
    mountLanding();
  });

  async function loadWithPassphraseRequired() {
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/meta')) {
        return Promise.resolve(jsonResponse({ passphrase_required: true }));
      }
      return new Promise(() => {});
    });
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();
  }

  it('stays hidden while the passphrase is short', async () => {
    await loadWithPassphraseRequired();

    const input = document.getElementById('passphrase');
    input.value = 'short value';
    input.dispatchEvent(new Event('input'));

    const hint = document.getElementById('passphrase-hint');
    expect(hint.hidden).toBe(true);
    expect(hint.textContent).toBe('');
    expect(hint.classList.contains('is-warning')).toBe(false);
  });

  it('reveals the warning hint at >=90% of the 200-char cap', async () => {
    await loadWithPassphraseRequired();

    const input = document.getElementById('passphrase');
    // 180 = 0.9 * 200, the documented warn-at threshold.
    input.value = 'a'.repeat(180);
    input.dispatchEvent(new Event('input'));

    const hint = document.getElementById('passphrase-hint');
    expect(hint.hidden).toBe(false);
    expect(hint.classList.contains('is-warning')).toBe(true);
    // Source-of-truth string from app/static/i18n/en.json hint.passphrase_approaching.
    expect(hint.textContent.length).toBeGreaterThan(0);
  });

  it('disappears again when the user backspaces below the threshold', async () => {
    await loadWithPassphraseRequired();

    const input = document.getElementById('passphrase');
    input.value = 'a'.repeat(180);
    input.dispatchEvent(new Event('input'));
    expect(document.getElementById('passphrase-hint').hidden).toBe(false);

    input.value = 'a'.repeat(50);
    input.dispatchEvent(new Event('input'));

    const hint = document.getElementById('passphrase-hint');
    expect(hint.hidden).toBe(true);
    expect(hint.classList.contains('is-warning')).toBe(false);
  });
});
