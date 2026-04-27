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
            <input type="password" id="passphrase" autocomplete="off">
            <button type="button" id="toggle-passphrase" class="input-action"
                    aria-label="show passphrase" aria-pressed="false">show</button>
          </div>
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

// ---------------------------------------------------------------------------
// Init / meta lookup
// ---------------------------------------------------------------------------

describe('reveal.js — init() / meta lookup', () => {
  beforeEach(() => {
    stubLocation();
    mountLanding();
  });

  it('shows state-gone when /meta returns 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response(null, { status: 404 })))
    );
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('state-gone').hidden).toBe(false);
    expect(document.getElementById('state-ready').hidden).toBe(true);
  });

  it('shows state-gone when /meta throws (network blip)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network')))
    );
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('state-gone').hidden).toBe(false);
  });

  it('unhides the passphrase wrap when /meta reports passphrase_required: true', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url) => {
        if (url.endsWith('/meta')) {
          return Promise.resolve(jsonResponse({ passphrase_required: true }));
        }
        return new Promise(() => {});
      })
    );
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('passphrase-wrap').hidden).toBe(false);
    expect(document.getElementById('state-ready').hidden).toBe(false);
  });

  it('keeps the passphrase wrap hidden when no passphrase is required', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url) => {
        if (url.endsWith('/meta')) {
          return Promise.resolve(jsonResponse({ passphrase_required: false }));
        }
        return new Promise(() => {});
      })
    );
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('passphrase-wrap').hidden).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Reveal success — text
// ---------------------------------------------------------------------------

describe('reveal.js — text reveal success path', () => {
  beforeEach(() => {
    stubLocation();
    mountLanding();
  });

  function metaPlusText(content) {
    return vi.fn((url) => {
      if (url.endsWith('/meta')) {
        return Promise.resolve(jsonResponse({ passphrase_required: false }));
      }
      return Promise.resolve(jsonResponse({ content_type: 'text', content }));
    });
  }

  it('renders the decrypted text into #revealed-text and shows state-text', async () => {
    vi.stubGlobal('fetch', metaPlusText('hello world'));
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('revealed-text').textContent).toBe('hello world');
    expect(document.getElementById('state-text').hidden).toBe(false);
    expect(document.getElementById('state-ready').hidden).toBe(true);
  });

  it('preserves whitespace and newlines verbatim (.textContent, not .innerHTML)', async () => {
    // The /pre/ tag in the fixture is the DOM-side preservation; the
    // assignment must use textContent so HTML in the secret isn't parsed.
    vi.stubGlobal('fetch', metaPlusText('line1\n  indented\n<b>not bold</b>'));
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();
    await flushAsync();

    const pre = document.getElementById('revealed-text');
    expect(pre.textContent).toBe('line1\n  indented\n<b>not bold</b>');
    // No <b> element should have been parsed -- it must be a literal string.
    expect(pre.querySelector('b')).toBeNull();
  });

  it('unhides the copy button and wires it to copy the revealed text', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    vi.stubGlobal('fetch', metaPlusText('the secret'));
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();
    await flushAsync();

    const copyBtn = document.getElementById('copy-btn');
    expect(copyBtn.hidden).toBe(false);

    copyBtn.click();
    await flushAsync();

    expect(writeText).toHaveBeenCalledWith('the secret');
  });
});

// ---------------------------------------------------------------------------
// Reveal success — image + zoom overlay
// ---------------------------------------------------------------------------

describe('reveal.js — image reveal success path', () => {
  beforeEach(() => {
    stubLocation();
    mountLanding();
  });

  // Smallest legal PNG (1x1 transparent), base64-encoded the way the server
  // sends it. Real pixels don't matter for these assertions; the test just
  // needs SOMETHING that goes through the data: URI assembly path.
  const TINY_PNG_B64 =
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+P+/HgAFhAJ/wlseKgAAAABJRU5ErkJggg==';

  function metaPlusImage(mime = 'image/png', content = TINY_PNG_B64) {
    return vi.fn((url) => {
      if (url.endsWith('/meta')) {
        return Promise.resolve(jsonResponse({ passphrase_required: false }));
      }
      return Promise.resolve(jsonResponse({ content_type: 'image', mime_type: mime, content }));
    });
  }

  async function revealImage(mime, content) {
    vi.stubGlobal('fetch', metaPlusImage(mime, content));
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();
    revealBtn().click();
    await flushAsync();
    await flushAsync();
  }

  it('sets the img src to a data: URI assembled from mime_type + base64 content', async () => {
    await revealImage('image/png');

    const img = document.getElementById('revealed-image');
    expect(img.getAttribute('src')).toBe(`data:image/png;base64,${TINY_PNG_B64}`);
    expect(document.getElementById('state-image').hidden).toBe(false);
    expect(document.getElementById('state-text').hidden).toBe(true);
  });

  it('adds the .wide class to #main-card so images get the wider layout', async () => {
    await revealImage('image/jpeg');

    expect(document.getElementById('main-card').classList.contains('wide')).toBe(true);
  });

  it('opens the zoom overlay on image click, locks body scroll, focuses close', async () => {
    await revealImage('image/png');

    const img = document.getElementById('revealed-image');
    const overlay = document.getElementById('zoom-overlay');
    const closeBtn = document.getElementById('zoom-close');
    const closeFocus = vi.spyOn(closeBtn, 'focus');

    img.click();

    expect(overlay.hidden).toBe(false);
    expect(document.body.style.overflow).toBe('hidden');
    expect(closeFocus).toHaveBeenCalled();
    // Zoom image carries the same data: URI as the thumbnail.
    expect(document.getElementById('zoom-image').getAttribute('src')).toBe(img.getAttribute('src'));
  });

  it('opens the zoom overlay via keyboard (Enter on the focused image)', async () => {
    await revealImage('image/png');

    const img = document.getElementById('revealed-image');
    img.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    );

    expect(document.getElementById('zoom-overlay').hidden).toBe(false);
  });

  it('opens the zoom overlay via keyboard (Space on the focused image)', async () => {
    await revealImage('image/png');

    const img = document.getElementById('revealed-image');
    img.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true }));

    expect(document.getElementById('zoom-overlay').hidden).toBe(false);
  });

  it('closes the zoom overlay when the close button is clicked, restores body scroll + focus', async () => {
    await revealImage('image/png');

    const img = document.getElementById('revealed-image');
    const overlay = document.getElementById('zoom-overlay');
    const closeBtn = document.getElementById('zoom-close');
    const thumbFocus = vi.spyOn(img, 'focus');

    img.click(); // open
    expect(overlay.hidden).toBe(false);

    closeBtn.click();

    expect(overlay.hidden).toBe(true);
    expect(document.body.style.overflow).toBe('');
    expect(thumbFocus).toHaveBeenCalled();
    // Zoom image's src is wiped on close so the next open re-renders fresh.
    expect(document.getElementById('zoom-image').getAttribute('src')).toBe('');
  });

  it('closes the zoom overlay when the backdrop (overlay itself) is clicked', async () => {
    await revealImage('image/png');

    const img = document.getElementById('revealed-image');
    const overlay = document.getElementById('zoom-overlay');
    img.click();
    expect(overlay.hidden).toBe(false);

    overlay.click();

    expect(overlay.hidden).toBe(true);
  });

  it('closes the zoom overlay on Escape', async () => {
    await revealImage('image/png');

    const img = document.getElementById('revealed-image');
    const overlay = document.getElementById('zoom-overlay');
    img.click();
    expect(overlay.hidden).toBe(false);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

    expect(overlay.hidden).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Reveal failure shapes (the ones not already covered above)
// ---------------------------------------------------------------------------

describe('reveal.js — failure shapes', () => {
  beforeEach(() => {
    stubLocation();
    mountLanding();
  });

  function metaThen(revealResp) {
    return vi.fn((url) => {
      if (url.endsWith('/meta')) {
        return Promise.resolve(jsonResponse({ passphrase_required: false }));
      }
      return revealResp();
    });
  }

  it('shows a rate-limit error on a 429', async () => {
    vi.stubGlobal(
      'fetch',
      metaThen(() => Promise.resolve(new Response(null, { status: 429 })))
    );
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();
    await flushAsync();

    const err = document.getElementById('reveal-error');
    expect(err.hidden).toBe(false);
    // Button restored so the user can retry once the limiter resets.
    expect(revealBtn().disabled).toBe(false);
  });

  it('shows the gone state on a 404 (the secret was never live or already burned)', async () => {
    vi.stubGlobal(
      'fetch',
      metaThen(() => Promise.resolve(new Response(null, { status: 404 })))
    );
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('state-gone').hidden).toBe(false);
  });

  it('shows a generic error on any other non-2xx (e.g. 500)', async () => {
    vi.stubGlobal(
      'fetch',
      metaThen(() => Promise.resolve(new Response(null, { status: 500 })))
    );
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('reveal-error').hidden).toBe(false);
    expect(revealBtn().disabled).toBe(false);
  });

  it('shows a network error and restores the button when fetch throws', async () => {
    vi.stubGlobal(
      'fetch',
      metaThen(() => Promise.reject(new Error('network')))
    );
    await loadModule('reveal');
    await flushAsync();
    await flushAsync();

    revealBtn().click();
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('reveal-error').hidden).toBe(false);
    expect(revealBtn().disabled).toBe(false);
  });
});
