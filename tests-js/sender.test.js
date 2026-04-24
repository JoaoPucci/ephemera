import { describe, it, expect, beforeEach, vi } from 'vitest';
import { flushAsync, jsonResponse, loadModule } from './helpers.js';

// Minimal sender.html fixture -- just the parts sender.js touches on load and
// during create-secret. We don't need the tracked-list header chrome because
// the IIFE guards against missing elements where it matters.
function mountSender() {
  document.body.innerHTML = `
    <button type="button" id="user-btn" class="user-btn" aria-label="Signed in as admin. Click to sign out.">
      <span class="user-dot"></span>
      <span id="user-name">…</span>
      <span class="user-sep">·</span>
      <span class="user-action">sign out</span>
    </button>
    <section id="tracked-section" hidden>
      <button type="button" id="tracked-header" aria-expanded="false"></button>
      <span id="tracked-count">0</span>
      <div id="tracked-body">
        <ul id="tracked-list"></ul>
        <button type="button" id="tracked-clear" hidden>
          <span id="tracked-clear-label">Clear past entries</span>
        </button>
      </div>
    </section>
    <div id="compose">
      <div class="tabs">
        <button class="tab active" data-tab="text" type="button">Text</button>
        <button class="tab" data-tab="image" type="button">Image</button>
      </div>
      <form id="secret-form">
        <section id="panel-text">
          <textarea id="content" name="content"></textarea>
        </section>
        <section id="panel-image" hidden>
          <div id="dropzone">
            <input type="file" id="file" hidden>
            <div id="preview" hidden>
              <span id="file-name"></span>
              <button type="button" id="clear-file">clear</button>
            </div>
          </div>
        </section>
        <select id="expires_in" name="expires_in"><option value="3600" selected>1h</option></select>
        <input type="text" id="passphrase" name="passphrase">
        <label><input type="checkbox" id="track" name="track"> Track</label>
        <div id="label-wrap" hidden>
          <input type="text" id="label">
        </div>
        <button type="submit" id="submit-btn">Create Secret</button>
        <p class="error" id="sender-error" hidden></p>
      </form>
    </div>
    <section id="result" hidden>
      <code id="result-url"></code>
      <button type="button" id="copy-url" class="copy-btn">Copy URL</button>
      <p id="result-expiry"></p>
      <div id="status-widget" hidden>
        <span id="status-value">pending</span>
        <span id="status-detail"></span>
      </div>
      <button type="button" id="create-another" class="link">Create another</button>
    </section>
  `;
}

// sender.js on load calls /api/me and /api/secrets/tracked. We wrap the
// user-supplied fetch mock so those endpoints get harmless default responses
// and the create-secret call path is what we assert on.
function stubSenderFetch(createHandler) {
  return vi.fn((url, opts) => {
    if (url === '/api/me') {
      return Promise.resolve(jsonResponse({ id: 1, username: 'admin', email: null }));
    }
    if (url === '/api/secrets/tracked') {
      return Promise.resolve(jsonResponse({ items: [] }));
    }
    if (url === '/api/secrets') {
      return createHandler(opts);
    }
    return Promise.resolve(new Response(null, { status: 404 }));
  });
}

function submitBtn() {
  return document.getElementById('submit-btn');
}

function submitForm() {
  document.getElementById('secret-form').dispatchEvent(
    new Event('submit', { cancelable: true, bubbles: true })
  );
}

describe('sender.js — create-secret in-flight guard', () => {
  beforeEach(() => {
    mountSender();
    document.getElementById('content').value = 'hello world';
  });

  it('fires exactly one /api/secrets POST even when submit is dispatched twice', async () => {
    // Hang the create call so we can observe the guard at work.
    const fetchMock = stubSenderFetch(() => new Promise(() => {}));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('sender');
    await flushAsync();

    submitForm();
    submitForm();
    await flushAsync();

    const createCalls = fetchMock.mock.calls.filter(([url]) => url === '/api/secrets');
    expect(createCalls.length).toBe(1);
  });

  it('disables the submit button and swaps the label while in flight', async () => {
    const fetchMock = stubSenderFetch(() => new Promise(() => {}));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('sender');
    await flushAsync();

    submitForm();
    await flushAsync();

    expect(submitBtn().disabled).toBe(true);
    expect(submitBtn().textContent).toBe('Creating…');
  });

  it('restores the button when the server returns a 4xx error', async () => {
    const fetchMock = stubSenderFetch(() =>
      Promise.resolve(jsonResponse({ detail: 'too big' }, 413))
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('sender');
    await flushAsync();

    submitForm();
    await flushAsync();
    await flushAsync();

    expect(submitBtn().disabled).toBe(false);
    expect(submitBtn().textContent).toBe('Create Secret');
    const err = document.getElementById('sender-error');
    expect(err.hidden).toBe(false);
    expect(err.textContent).toContain('too big');
  });

  it('hides the compose form and shows the result on success', async () => {
    const fetchMock = stubSenderFetch(() =>
      Promise.resolve(
        jsonResponse({
          url: 'https://example/s/tok#key',
          id: 'deadbeef',
          expires_at: '2099-01-01T00:00:00Z',
        })
      )
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('sender');
    await flushAsync();

    submitForm();
    await flushAsync();
    await flushAsync();

    expect(document.getElementById('compose').hidden).toBe(true);
    expect(document.getElementById('result').hidden).toBe(false);
    expect(document.getElementById('result-url').textContent).toBe('https://example/s/tok#key');
  });

  it('restores the submit button after "Create another", so the recycled form is usable', async () => {
    // Regression guard: the in-flight guard used to leave the button stuck
    // on "Creating…" after a successful create; clicking "Create another"
    // then surfaced a disabled button with the wrong label.
    const fetchMock = stubSenderFetch(() =>
      Promise.resolve(
        jsonResponse({
          url: 'https://example/s/tok#key',
          id: 'deadbeef',
          expires_at: '2099-01-01T00:00:00Z',
        })
      )
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('sender');
    await flushAsync();

    submitForm();
    await flushAsync();
    await flushAsync();

    document.getElementById('create-another').click();

    expect(document.getElementById('compose').hidden).toBe(false);
    expect(document.getElementById('result').hidden).toBe(true);
    expect(submitBtn().disabled).toBe(false);
    expect(submitBtn().textContent).toBe('Create Secret');
  });

  it('does not fire a fetch when the textarea is empty', async () => {
    document.getElementById('content').value = '';
    const fetchMock = stubSenderFetch(() => new Promise(() => {}));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('sender');
    await flushAsync();

    submitForm();
    await flushAsync();

    const createCalls = fetchMock.mock.calls.filter(([url]) => url === '/api/secrets');
    expect(createCalls.length).toBe(0);
    expect(document.getElementById('sender-error').hidden).toBe(false);
  });
});


describe('sender.js — user button sign-out two-click confirm', () => {
  beforeEach(() => {
    mountSender();
  });

  // Extend the default sender stub with a /send/logout handler. We hang the
  // logout promise so the handler's trailing window.location.reload() is
  // never reached -- jsdom's reload is non-configurable and can't be spied
  // on cleanly, and we only need to verify the fetch was fired. Hanging the
  // promise is the same trick tracked-cancel's existing tests would use.
  function stubSenderFetchWithLogout(createHandler) {
    return vi.fn((url, opts) => {
      if (url === '/send/logout') return new Promise(() => {});  // hang
      if (url === '/api/me') {
        return Promise.resolve(jsonResponse({ id: 1, username: 'admin', email: null }));
      }
      if (url === '/api/secrets/tracked') {
        return Promise.resolve(jsonResponse({ items: [] }));
      }
      if (url === '/api/secrets') {
        return createHandler ? createHandler(opts) : new Promise(() => {});
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
  }

  function userBtn() { return document.getElementById('user-btn'); }
  function actionLabel() { return userBtn().querySelector('.user-action').textContent; }

  it('first click arms the button without firing logout', async () => {
    const fetchMock = stubSenderFetchWithLogout();
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('sender');
    await flushAsync();

    userBtn().click();
    await flushAsync();

    expect(userBtn().classList.contains('armed')).toBe(true);
    expect(actionLabel()).toBe('really sign out?');
    // aria-label flips so screen readers get the re-prompt
    expect(userBtn().getAttribute('aria-label')).toContain('confirm');
    // No /send/logout POST yet
    const logoutCalls = fetchMock.mock.calls.filter(([url]) => url === '/send/logout');
    expect(logoutCalls.length).toBe(0);
  });

  it('second click while armed fires the logout', async () => {
    const fetchMock = stubSenderFetchWithLogout();
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('sender');
    await flushAsync();

    userBtn().click();          // arm
    await flushAsync();
    userBtn().click();          // confirm
    await flushAsync();

    const logoutCalls = fetchMock.mock.calls.filter(([url]) => url === '/send/logout');
    expect(logoutCalls.length).toBe(1);
    expect(logoutCalls[0][1]?.method).toBe('POST');
  });

  it('auto-disarms after the 3s timeout and restores the original label', async () => {
    vi.useFakeTimers();
    const fetchMock = stubSenderFetchWithLogout();
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('sender');
    await vi.runOnlyPendingTimersAsync();

    userBtn().click();  // arm
    expect(userBtn().classList.contains('armed')).toBe(true);
    expect(actionLabel()).toBe('really sign out?');

    vi.advanceTimersByTime(3000);

    expect(userBtn().classList.contains('armed')).toBe(false);
    expect(actionLabel()).toBe('sign out');
    // Logout still not fired -- the timeout disarmed, no commit.
    const logoutCalls = fetchMock.mock.calls.filter(([url]) => url === '/send/logout');
    expect(logoutCalls.length).toBe(0);

    vi.useRealTimers();
  });
});
