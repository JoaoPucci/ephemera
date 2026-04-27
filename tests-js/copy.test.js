import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadModule } from './helpers.js';

// ---------------------------------------------------------------------------
// copy.js exposes a single export: copyWithFeedback(button, text). The
// function attempts navigator.clipboard.writeText() first and falls back to
// the document.execCommand('copy') trick when the modern API is unavailable.
// On either branch it flips the button's label + class for ~1.8s, gating
// re-entry through a data-busy="1" sticky flag.
//
// loadModule('copy') re-imports the module against a fresh DOM and a fresh
// i18n stub (installI18nStub from helpers.js, sourced from the real en.json),
// so the tests exercise the actual ship strings ("Copied", "Copy failed").
// ---------------------------------------------------------------------------

function makeButton(initialLabel = 'Copy URL') {
  document.body.innerHTML = `<button id="copy-btn">${initialLabel}</button>`;
  return document.getElementById('copy-btn');
}

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// navigator.clipboard.writeText path (modern browsers + jsdom-stubbed)
// ---------------------------------------------------------------------------

describe('copy.js — navigator.clipboard.writeText path', () => {
  let writeText;

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
  });

  it('calls writeText with the supplied text and flips the button to "Copied"', async () => {
    const { copyWithFeedback } = await loadModule('copy');
    const btn = makeButton();

    await copyWithFeedback(btn, 'https://host/s/abc#KEY');

    expect(writeText).toHaveBeenCalledWith('https://host/s/abc#KEY');
    expect(btn.textContent).toBe('Copied');
    expect(btn.classList.contains('copied')).toBe(true);
    expect(btn.classList.contains('copy-error')).toBe(false);
    expect(btn.dataset.busy).toBe('1');
  });

  it('sets aria-live=polite on the button so AT users hear the feedback', async () => {
    const { copyWithFeedback } = await loadModule('copy');
    const btn = makeButton();

    await copyWithFeedback(btn, 'x');

    expect(btn.getAttribute('aria-live')).toBe('polite');
  });

  it('flips to "Copy failed" + .copy-error when writeText rejects', async () => {
    writeText.mockRejectedValue(new Error('blocked'));
    const { copyWithFeedback } = await loadModule('copy');
    const btn = makeButton();

    await copyWithFeedback(btn, 'x');

    expect(btn.textContent).toBe('Copy failed');
    expect(btn.classList.contains('copy-error')).toBe(true);
    expect(btn.classList.contains('copied')).toBe(false);
  });

  it('busy guard: a second call while the first is in flight is a no-op', async () => {
    // Hang writeText so the first call stays in flight; verify the second
    // call returns immediately without invoking writeText again.
    let resolveFirst;
    writeText.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolveFirst = r;
        })
    );
    const { copyWithFeedback } = await loadModule('copy');
    const btn = makeButton();

    const inFlight = copyWithFeedback(btn, 'first');
    // Don't await -- it's still pending. Immediately fire a second call.
    await copyWithFeedback(btn, 'second');

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith('first');

    // Drain the first call so afterEach restores cleanly.
    resolveFirst();
    await inFlight;
  });
});

// ---------------------------------------------------------------------------
// Restore-on-timeout: label, class, busy flag all roll back after 1.8s
// ---------------------------------------------------------------------------

describe('copy.js — 1.8s restore', () => {
  it('restores label, removes .copied, and clears data-busy after the timeout', async () => {
    vi.useFakeTimers();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    const { copyWithFeedback } = await loadModule('copy');
    const btn = makeButton('Copy URL');

    const p = copyWithFeedback(btn, 'x');
    await vi.advanceTimersByTimeAsync(0); // let writeText's microtask resolve
    await p;

    // Mid-flash state
    expect(btn.textContent).toBe('Copied');
    expect(btn.classList.contains('copied')).toBe(true);
    expect(btn.dataset.busy).toBe('1');

    vi.advanceTimersByTime(1800);

    expect(btn.textContent).toBe('Copy URL');
    expect(btn.classList.contains('copied')).toBe(false);
    expect(btn.dataset.busy).toBeUndefined();
  });

  it('restores after a failure flash too (.copy-error and label revert together)', async () => {
    vi.useFakeTimers();
    const writeText = vi.fn().mockRejectedValue(new Error('blocked'));
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    const { copyWithFeedback } = await loadModule('copy');
    const btn = makeButton('Copy passphrase');

    const p = copyWithFeedback(btn, 'x');
    await vi.advanceTimersByTimeAsync(0);
    await p;

    expect(btn.classList.contains('copy-error')).toBe(true);
    expect(btn.textContent).toBe('Copy failed');

    vi.advanceTimersByTime(1800);

    expect(btn.textContent).toBe('Copy passphrase');
    expect(btn.classList.contains('copy-error')).toBe(false);
    expect(btn.dataset.busy).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// document.execCommand fallback (older browsers / non-secure-context paths)
// ---------------------------------------------------------------------------

describe('copy.js — execCommand fallback', () => {
  // Strip clipboard.writeText so the function takes the legacy path.
  beforeEach(() => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined });
  });

  it('builds a hidden textarea, calls execCommand("copy"), and removes the textarea', async () => {
    const exec = vi.fn(() => true);
    document.execCommand = exec;
    const { copyWithFeedback } = await loadModule('copy');
    const btn = makeButton();

    await copyWithFeedback(btn, 'fallback-text');

    // The textarea is appended + removed inside the function; after it
    // returns, no leftover textareas should be in the DOM. Only the
    // button (from the fixture) should remain.
    expect(document.querySelectorAll('textarea').length).toBe(0);
    expect(exec).toHaveBeenCalledWith('copy');
    expect(btn.textContent).toBe('Copied');
    expect(btn.classList.contains('copied')).toBe(true);
  });

  it('flips to "Copy failed" when execCommand returns false', async () => {
    document.execCommand = vi.fn(() => false);
    const { copyWithFeedback } = await loadModule('copy');
    const btn = makeButton();

    await copyWithFeedback(btn, 'x');

    expect(btn.textContent).toBe('Copy failed');
    expect(btn.classList.contains('copy-error')).toBe(true);
  });

  it('flips to "Copy failed" when execCommand throws (security-policy refusal)', async () => {
    document.execCommand = vi.fn(() => {
      throw new Error('not allowed');
    });
    const { copyWithFeedback } = await loadModule('copy');
    const btn = makeButton();

    await copyWithFeedback(btn, 'x');

    expect(btn.textContent).toBe('Copy failed');
    expect(btn.classList.contains('copy-error')).toBe(true);
  });
});
