import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { loadModule } from './helpers.js';

// ---------------------------------------------------------------------------
// hints.js exposes bindCounterHint + bindPassphraseHint. The full counter
// state machine (idle / counter-shown / warning / error / paste-trim /
// paste-large) is exercised through tests-js/sender.test.js's char-limit
// tests. This dedicated file pins the smaller paths those integration
// tests didn't reach: the >=1MB branch in _formatBytes, the "small paste
// under threshold" branch in bindCounterHint's paste handler, and the
// "field opted out of paste-large" branch (Infinity threshold).
// ---------------------------------------------------------------------------

function mountField({ tag = 'textarea', maxlength = 100 } = {}) {
  document.body.innerHTML = `
    <${tag} id="field" maxlength="${maxlength}"></${tag}>
    <p id="hint" hidden></p>
  `;
  return {
    input: document.getElementById('field'),
    hint: document.getElementById('hint'),
  };
}

function dispatchPaste(input, text) {
  const data = new DataTransfer();
  data.setData('text', text);
  const ev = new Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(ev, 'clipboardData', { value: data });
  input.dispatchEvent(ev);
  // Browser would normally insert the text. Simulate the post-paste DOM
  // state: input.value gains the pasted content (truncated by maxlength).
  const max = parseInt(input.getAttribute('maxlength') ?? '0', 10) || Infinity;
  input.value = (input.value + text).slice(0, max);
  // Fire the input event the binder listens for, with the inputType the
  // paste-override branch checks against.
  input.dispatchEvent(new InputEvent('input', { inputType: 'insertFromPaste', bubbles: true }));
}

afterEach(() => {
  document.body.innerHTML = '';
});

beforeEach(() => {
  // jsdom doesn't ship DataTransfer pre-21; supply a minimal stand-in
  // for the paste-event payload. The hints binder reads
  // `e.clipboardData.getData('text')` so we only need that one method
  // to behave.
  if (typeof globalThis.DataTransfer === 'undefined') {
    globalThis.DataTransfer = class {
      _data = '';
      setData(_type, value) {
        this._data = value;
      }
      getData() {
        return this._data;
      }
    };
  }
});

describe('hints.js — _formatBytes (covered transitively via the paste-large warning)', () => {
  it('renders bytes for small pastes (B unit)', async () => {
    const { input, hint } = mountField();
    const { bindCounterHint } = await loadModule('sender/hints');

    bindCounterHint(input, hint, 100, { pasteLargeThreshold: 10 });
    // Paste 20 bytes (20 ASCII chars). Triggers the warning branch which
    // calls _formatBytes(20) -> "20 B". The hint message includes that.
    dispatchPaste(input, 'x'.repeat(20));

    expect(hint.textContent).toContain('20 B');
  });

  it('renders kilobytes for paste sizes >= 1024 B (KB unit)', async () => {
    const { input, hint } = mountField({ maxlength: 100_000 });
    const { bindCounterHint } = await loadModule('sender/hints');

    bindCounterHint(input, hint, 100_000, { pasteLargeThreshold: 1000 });
    // 5 KB ASCII -> _formatBytes(5120) -> "5 KB"
    dispatchPaste(input, 'a'.repeat(5120));

    expect(hint.textContent).toContain('5 KB');
  });

  it('renders megabytes for paste sizes >= 1 MiB (MB unit)', async () => {
    const { input, hint } = mountField({ maxlength: 5_000_000 });
    const { bindCounterHint } = await loadModule('sender/hints');

    bindCounterHint(input, hint, 5_000_000, { pasteLargeThreshold: 1000 });
    // 1.5 MB ASCII paste -> bytes ~ 1_572_864 -> formatBytes returns
    // "1.5 MB" (Math.round(1.5*10)/10 = 1.5). Triggers the line-43
    // branch in _formatBytes that the smaller pastes don't reach.
    dispatchPaste(input, 'a'.repeat(1_572_864));

    expect(hint.textContent).toContain('MB');
  });
});

describe('hints.js — paste handler else-branches', () => {
  it('paste BELOW pasteLargeThreshold (textarea path): no warning shown', async () => {
    const { input, hint } = mountField({ maxlength: 1000 });
    const { bindCounterHint } = await loadModule('sender/hints');

    bindCounterHint(input, hint, 1000, {
      counterAt: 0.99,
      warningAt: 1.0,
      pasteLargeThreshold: 500, // 500 bytes
    });
    // Paste 100 bytes: well under pasteLargeThreshold AND under the
    // counterAt threshold. No paste-trim, no paste-large warning, no
    // counter -> hint stays in idle state (hidden).
    dispatchPaste(input, 'x'.repeat(100));

    expect(hint.hidden).toBe(true);
  });

  it('label/passphrase field (pasteLargeThreshold = Infinity): no large-paste warning ever fires', async () => {
    // The label field opts out of the paste-large warning by leaving
    // pasteLargeThreshold at the default Infinity. A paste that's
    // small (under maxlength) should land silently -- no warning, no
    // error, just a plain idle hint. Pins the "outer else" branch
    // that returns pasteOverrideMessage = null when pasteLargeThreshold
    // is Infinity.
    const { input, hint } = mountField({ tag: 'input', maxlength: 60 });
    const { bindCounterHint } = await loadModule('sender/hints');

    bindCounterHint(input, hint, 60, {
      counterAt: 0.99,
      warningAt: 1.0,
      useShortTrimMessage: true,
      // No pasteLargeThreshold -> defaults to Infinity (the field opts out).
    });
    dispatchPaste(input, 'short label paste');

    // The paste was below maxlength, no warning was set, the hint stays
    // idle (no class modifiers, no error text).
    expect(hint.classList.contains('is-warning')).toBe(false);
    expect(hint.classList.contains('is-error')).toBe(false);
  });
});

describe('hints.js — paste handler counter rendering', () => {
  it('paste that lands above counterAt but under cap: counter shows without warning class', async () => {
    // Sanity: covers the counter-shown branch at the regular counter
    // threshold (75% of cap by default). Existing sender.test.js
    // already pins this against the textarea field, but the dedicated
    // hint tests here ought to mirror the contract independently.
    const { input, hint } = mountField({ maxlength: 100 });
    const { bindCounterHint } = await loadModule('sender/hints');

    bindCounterHint(input, hint, 100, {
      counterAt: 0.75,
      warningAt: 0.95,
    });
    // Type 80 chars -- above counterAt(75), below warningAt(95).
    input.value = 'a'.repeat(80);
    input.dispatchEvent(new InputEvent('input', { inputType: 'insertText', bubbles: true }));

    expect(hint.hidden).toBe(false);
    expect(hint.classList.contains('is-warning')).toBe(false);
    expect(hint.classList.contains('is-error')).toBe(false);
    expect(hint.textContent).toContain('80');
  });
});
